"""Quota-aware token selection for image-capable requests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional, Set

from app.core.config import get_config
from app.core.exceptions import AppException, ErrorType
from app.core.logger import logger
from app.services.grok.services.model import ModelService
from app.services.reverse.rate_limits import RateLimitsReverse
from app.services.reverse.utils.session import ResettableSession

IMAGE_LIMIT_ERROR_CODE = "image_generation_limit_reached"
IMAGE_LIMIT_SINGLE_MESSAGE = "当前账号生图额度已达上限，请稍后再试。"
IMAGE_LIMIT_ALL_MESSAGE = "所有可用账号的生图额度均已达上限，请稍后再试。"
IMAGE_QUOTA_SLOT = "grok-imagine-1.0"
AUTO_IMAGE_QUOTA_SLOT = "auto"
DEFAULT_IMAGE_PROBE_TTL_SEC = 30


@dataclass(frozen=True)
class QuotaRequirement:
    slot: str
    probe_models: tuple[str, ...]
    request_kind: str = "DEFAULT"


@dataclass
class QuotaProbeResult:
    slot: str
    probe_model: str
    source_model: str
    remaining_queries: Optional[int]
    wait_time_seconds: Optional[int]
    checked_at: int
    cache_hit: bool
    exhausted: bool
    known: bool
    error: str = ""


@dataclass
class TokenSelectionResult:
    token: Optional[str]
    total_candidates: int


def auto_image_quota_requirement() -> QuotaRequirement:
    return QuotaRequirement(slot=AUTO_IMAGE_QUOTA_SLOT, probe_models=("auto",))


def image_quota_requirement() -> QuotaRequirement:
    return QuotaRequirement(
        slot=IMAGE_QUOTA_SLOT,
        probe_models=(
            "grok-imagine-1.0",
            "grok-imagine-1.0-fast",
            "grok-imagine-1.0-edit",
        ),
    )


def quota_requirement_for_model(model_id: str) -> Optional[QuotaRequirement]:
    if str(model_id or "").strip() == "grok-auto":
        return auto_image_quota_requirement()

    model_info = ModelService.get(model_id)
    if model_info and (model_info.is_image or model_info.is_image_edit):
        return image_quota_requirement()
    return None


def image_limit_exception(total_candidates: int) -> AppException:
    message = (
        IMAGE_LIMIT_SINGLE_MESSAGE
        if int(total_candidates or 0) <= 1
        else IMAGE_LIMIT_ALL_MESSAGE
    )
    return AppException(
        message=message,
        error_type=ErrorType.RATE_LIMIT.value,
        code=IMAGE_LIMIT_ERROR_CODE,
        status_code=429,
    )


def is_image_limit_exception(error: Exception) -> bool:
    return isinstance(error, AppException) and error.code == IMAGE_LIMIT_ERROR_CODE


def all_candidate_tokens_exhausted(
    total_candidates: int, exhausted_tokens: Optional[Set[str]]
) -> bool:
    return int(total_candidates or 0) > 0 and len(exhausted_tokens or set()) >= int(
        total_candidates or 0
    )


def _ttl_ms() -> int:
    value = get_config("quota.image_probe_ttl_sec", DEFAULT_IMAGE_PROBE_TTL_SEC)
    try:
        seconds = max(0.0, float(value))
    except Exception:
        seconds = float(DEFAULT_IMAGE_PROBE_TTL_SEC)
    return int(seconds * 1000)


def _extract_remaining_queries(payload: dict[str, Any]) -> Optional[int]:
    value = payload.get("remainingQueries")
    if value is None:
        value = payload.get("remainingTokens")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _extract_wait_seconds(payload: dict[str, Any]) -> Optional[int]:
    value = payload.get("waitTimeSeconds")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _probe_known(payload: dict[str, Any]) -> bool:
    return (
        _extract_remaining_queries(payload) is not None
        or _extract_wait_seconds(payload) is not None
    )


def _build_probe_result(
    requirement: QuotaRequirement,
    payload: dict[str, Any],
    *,
    probe_model: str,
    cache_hit: bool,
    error: str = "",
) -> QuotaProbeResult:
    checked_at = payload.get("checkedAt")
    try:
        checked_at = int(checked_at)
    except (TypeError, ValueError):
        checked_at = int(time.time() * 1000)

    remaining_queries = _extract_remaining_queries(payload)
    wait_time_seconds = _extract_wait_seconds(payload)
    known = _probe_known(payload)
    exhausted = bool(
        known
        and (
            (remaining_queries is not None and remaining_queries <= 0)
            or (wait_time_seconds is not None and wait_time_seconds > 0)
        )
    )
    return QuotaProbeResult(
        slot=requirement.slot,
        probe_model=probe_model,
        source_model=str(payload.get("sourceModelName") or probe_model),
        remaining_queries=remaining_queries,
        wait_time_seconds=wait_time_seconds,
        checked_at=checked_at,
        cache_hit=cache_hit,
        exhausted=exhausted,
        known=known,
        error=error,
    )


async def probe_quota(
    token_mgr,
    token: str,
    requirement: QuotaRequirement,
    *,
    force_refresh: bool = False,
) -> QuotaProbeResult:
    now_ms = int(time.time() * 1000)
    cached = token_mgr.get_rate_limit_cache_entry(token, requirement.slot)
    ttl_ms = _ttl_ms()

    if (
        cached
        and not force_refresh
        and now_ms - int(cached.get("checkedAt") or 0) <= ttl_ms
    ):
        result = _build_probe_result(
            requirement,
            cached,
            probe_model=str(cached.get("sourceModelName") or requirement.probe_models[0]),
            cache_hit=True,
        )
        logger.debug(
            "Quota probe cache hit",
            extra={
                "quota_slot": requirement.slot,
                "quota_probe_model": result.probe_model,
                "remaining_queries": result.remaining_queries,
                "wait_time_seconds": result.wait_time_seconds,
                "image_limit_detected": result.exhausted,
                "token": f"{token[:10]}...",
            },
        )
        return result

    last_error = ""
    for probe_model in requirement.probe_models:
        try:
            async with ResettableSession() as session:
                response = await RateLimitsReverse.request(
                    session,
                    token,
                    model_name=probe_model,
                    request_kind=requirement.request_kind,
                )
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {}
            payload = dict(payload)
            payload["checkedAt"] = now_ms
            if probe_model != requirement.slot:
                payload["sourceModelName"] = payload.get("sourceModelName") or probe_model
            token_mgr.update_rate_limit_cache_entry(
                token,
                requirement.slot,
                payload,
                checked_at=now_ms,
            )
            result = _build_probe_result(
                requirement,
                payload,
                probe_model=probe_model,
                cache_hit=False,
            )
            logger.info(
                "Quota probe refreshed",
                extra={
                    "quota_slot": requirement.slot,
                    "quota_probe_model": probe_model,
                    "remaining_queries": result.remaining_queries,
                    "wait_time_seconds": result.wait_time_seconds,
                    "image_limit_detected": result.exhausted,
                    "token": f"{token[:10]}...",
                },
            )
            return result
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "Quota probe failed",
                extra={
                    "quota_slot": requirement.slot,
                    "quota_probe_model": probe_model,
                    "token": f"{token[:10]}...",
                    "error": last_error,
                },
            )

    return QuotaProbeResult(
        slot=requirement.slot,
        probe_model=requirement.probe_models[0],
        source_model=requirement.probe_models[0],
        remaining_queries=None,
        wait_time_seconds=None,
        checked_at=now_ms,
        cache_hit=False,
        exhausted=False,
        known=False,
        error=last_error,
    )


async def confirm_quota_exhausted(
    token_mgr,
    token: str,
    requirement: QuotaRequirement,
    exhausted_tokens: Optional[Set[str]] = None,
) -> bool:
    result = await probe_quota(token_mgr, token, requirement, force_refresh=True)
    exhausted = result.known and result.exhausted
    if exhausted and exhausted_tokens is not None:
        exhausted_tokens.add(token)
    return exhausted


def count_candidate_tokens(token_mgr, model_id: str) -> int:
    consumed_mode = False
    if hasattr(token_mgr, "_is_consumed_mode"):
        try:
            consumed_mode = bool(token_mgr._is_consumed_mode())
        except Exception:
            consumed_mode = False

    seen: set[str] = set()
    for pool_name in ModelService.pool_candidates_for_model(model_id):
        pool = token_mgr.pools.get(pool_name)
        if not pool:
            continue
        for token_info in pool.list():
            if token_info.token in seen:
                continue
            if not token_info.is_available(consumed_mode=consumed_mode):
                continue
            seen.add(token_info.token)
    return len(seen)


async def select_token_for_requirement(
    token_mgr,
    model_id: str,
    *,
    tried: Set[str],
    requirement: Optional[QuotaRequirement] = None,
    preferred: Optional[str] = None,
    prefer_tags: Optional[Set[str]] = None,
    exhausted_tokens: Optional[Set[str]] = None,
) -> TokenSelectionResult:
    total_candidates = count_candidate_tokens(token_mgr, model_id)
    excluded = set(tried or set())
    if exhausted_tokens:
        excluded.update(exhausted_tokens)

    async def _accept(candidate: Optional[str]) -> Optional[str]:
        if not candidate or candidate in excluded:
            return None
        if not requirement:
            return candidate

        probe = await probe_quota(token_mgr, candidate, requirement)
        if probe.known and probe.exhausted:
            if exhausted_tokens is not None:
                exhausted_tokens.add(candidate)
            excluded.add(candidate)
            logger.info(
                "Token skipped by quota prefilter",
                extra={
                    "quota_slot": requirement.slot,
                    "quota_probe_model": probe.source_model,
                    "remaining_queries": probe.remaining_queries,
                    "wait_time_seconds": probe.wait_time_seconds,
                    "token": f"{candidate[:10]}...",
                },
            )
            return None
        return candidate

    if preferred and preferred not in excluded:
        accepted = await _accept(preferred)
        if accepted:
            if hasattr(token_mgr, "bind_token_context"):
                try:
                    token_mgr.bind_token_context(accepted)
                except Exception:
                    pass
            return TokenSelectionResult(token=accepted, total_candidates=total_candidates)

    for pool_name in ModelService.pool_candidates_for_model(model_id):
        while True:
            token_info = token_mgr.get_token_info(
                pool_name,
                exclude=excluded,
                prefer_tags=prefer_tags,
                model_id=model_id,
            )
            if not token_info:
                break

            candidate = token_info.token
            accepted = await _accept(candidate)
            if accepted:
                return TokenSelectionResult(
                    token=accepted,
                    total_candidates=total_candidates,
                )
            excluded.add(candidate)

    return TokenSelectionResult(token=None, total_candidates=total_candidates)


__all__ = [
    "AUTO_IMAGE_QUOTA_SLOT",
    "DEFAULT_IMAGE_PROBE_TTL_SEC",
    "IMAGE_LIMIT_ALL_MESSAGE",
    "IMAGE_LIMIT_ERROR_CODE",
    "IMAGE_LIMIT_SINGLE_MESSAGE",
    "IMAGE_QUOTA_SLOT",
    "QuotaProbeResult",
    "QuotaRequirement",
    "TokenSelectionResult",
    "all_candidate_tokens_exhausted",
    "auto_image_quota_requirement",
    "confirm_quota_exhausted",
    "count_candidate_tokens",
    "image_limit_exception",
    "image_quota_requirement",
    "is_image_limit_exception",
    "probe_quota",
    "quota_requirement_for_model",
    "select_token_for_requirement",
]
