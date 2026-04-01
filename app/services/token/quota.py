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
TEXT_AUTO_QUOTA_SLOT = "auto"
TEXT_GROK3_QUOTA_SLOT = "grok-3"
TEXT_GROK4_QUOTA_SLOT = "grok-4"
VIDEO_QUOTA_SLOT = "grok-imagine-1.0-video"
DEFAULT_IMAGE_PROBE_TTL_SEC = 30
DEFAULT_TEXT_PROBE_TTL_SEC = 15
RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED = "confirmed_exhausted"
RATE_LIMIT_ACTION_RETRY_SAME_TOKEN = "retry_same_token"
RATE_LIMIT_ACTION_SOFT_COOLING = "soft_cooled"


@dataclass(frozen=True)
class QuotaRequirement:
    slot: str
    probe_models: tuple[str, ...]
    request_kind: str = "DEFAULT"
    ttl_config_key: str = "quota.image_probe_ttl_sec"
    ttl_default_sec: float = DEFAULT_IMAGE_PROBE_TTL_SEC


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


@dataclass
class RateLimitResolution:
    action: str
    probe: Optional["QuotaProbeResult"]
    retry_after_seconds: float = 0.0


def auto_image_quota_requirement() -> QuotaRequirement:
    return QuotaRequirement(
        slot=AUTO_IMAGE_QUOTA_SLOT,
        probe_models=("auto",),
        ttl_config_key="quota.image_probe_ttl_sec",
        ttl_default_sec=DEFAULT_IMAGE_PROBE_TTL_SEC,
    )


def image_quota_requirement() -> QuotaRequirement:
    return QuotaRequirement(
        slot=IMAGE_QUOTA_SLOT,
        probe_models=(
            "grok-imagine-1.0",
            "grok-imagine-1.0-fast",
            "grok-imagine-1.0-edit",
        ),
        ttl_config_key="quota.image_probe_ttl_sec",
        ttl_default_sec=DEFAULT_IMAGE_PROBE_TTL_SEC,
    )


def video_quota_requirement() -> QuotaRequirement:
    return QuotaRequirement(
        slot=VIDEO_QUOTA_SLOT,
        probe_models=("grok-imagine-1.0-video",),
        ttl_config_key="quota.image_probe_ttl_sec",
        ttl_default_sec=DEFAULT_IMAGE_PROBE_TTL_SEC,
    )


def text_auto_quota_requirement() -> QuotaRequirement:
    return QuotaRequirement(
        slot=TEXT_AUTO_QUOTA_SLOT,
        probe_models=("auto",),
        ttl_config_key="quota.text_probe_ttl_sec",
        ttl_default_sec=DEFAULT_TEXT_PROBE_TTL_SEC,
    )


def text_grok3_quota_requirement() -> QuotaRequirement:
    return QuotaRequirement(
        slot=TEXT_GROK3_QUOTA_SLOT,
        probe_models=("grok-3",),
        ttl_config_key="quota.text_probe_ttl_sec",
        ttl_default_sec=DEFAULT_TEXT_PROBE_TTL_SEC,
    )


def text_grok4_quota_requirement() -> QuotaRequirement:
    return QuotaRequirement(
        slot=TEXT_GROK4_QUOTA_SLOT,
        probe_models=("grok-4",),
        ttl_config_key="quota.text_probe_ttl_sec",
        ttl_default_sec=DEFAULT_TEXT_PROBE_TTL_SEC,
    )


def quota_requirement_for_model(model_id: str) -> Optional[QuotaRequirement]:
    if str(model_id or "").strip() == "grok-auto":
        return auto_image_quota_requirement()

    model_info = ModelService.get(model_id)
    if model_info and (model_info.is_image or model_info.is_image_edit):
        return image_quota_requirement()
    return None


def rate_limit_requirement_for_model(model_id: str) -> Optional[QuotaRequirement]:
    model = str(model_id or "").strip()
    if model == "grok-auto":
        return text_auto_quota_requirement()
    if model == "grok-3-fast":
        return text_grok3_quota_requirement()
    if model in {"grok-4-expert", "grok-4-heavy"}:
        return text_grok4_quota_requirement()

    model_info = ModelService.get(model)
    if model_info and (model_info.is_image or model_info.is_image_edit):
        return image_quota_requirement()
    if model_info and model_info.is_video:
        return video_quota_requirement()
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


def _ttl_ms(requirement: QuotaRequirement) -> int:
    value = get_config(requirement.ttl_config_key, requirement.ttl_default_sec)
    try:
        seconds = max(0.0, float(value))
    except Exception:
        seconds = float(requirement.ttl_default_sec)
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
    ttl_ms = _ttl_ms(requirement)

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


def _probe_result_payload(
    probe: QuotaProbeResult,
    *,
    action: str,
    model_id: str,
) -> dict[str, Any]:
    return {
        "action": action,
        "model_id": model_id,
        "slot": probe.slot,
        "probe_model": probe.probe_model,
        "source_model": probe.source_model,
        "remaining_queries": probe.remaining_queries,
        "wait_time_seconds": probe.wait_time_seconds,
        "known": probe.known,
        "exhausted": probe.exhausted,
        "cache_hit": probe.cache_hit,
        "checked_at": probe.checked_at,
        "error": probe.error,
    }


def _rate_limit_retry_delay_seconds() -> float:
    value = get_config("retry.retry_backoff_base", 0.5)
    try:
        seconds = float(value)
    except Exception:
        seconds = 0.5
    return min(max(seconds, 0.25), 2.0)


async def resolve_rate_limit_hit(
    token_mgr,
    token: str,
    model_id: str,
    *,
    requirement: Optional[QuotaRequirement] = None,
    exhausted_tokens: Optional[Set[str]] = None,
) -> RateLimitResolution:
    resolved_requirement = requirement or rate_limit_requirement_for_model(model_id)
    logger.warning(
        "rate_limit_hit",
        extra={
            "model": model_id,
            "token": f"{token[:10]}...",
            "quota_slot": getattr(resolved_requirement, "slot", ""),
        },
    )

    if not bool(get_config("token.rate_limit_probe_on_429_enabled", True)):
        await token_mgr.mark_rate_limited(token)
        if exhausted_tokens is not None:
            exhausted_tokens.add(token)
        return RateLimitResolution(
            action=RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED,
            probe=None,
        )

    if not resolved_requirement:
        await token_mgr.mark_rate_limited_soft(token)
        return RateLimitResolution(
            action=RATE_LIMIT_ACTION_SOFT_COOLING,
            probe=None,
        )

    logger.info(
        "rate_limit_probe_started",
        extra={
            "model": model_id,
            "token": f"{token[:10]}...",
            "quota_slot": resolved_requirement.slot,
        },
    )
    probe = await probe_quota(token_mgr, token, resolved_requirement, force_refresh=True)

    if probe.known and probe.exhausted:
        payload = _probe_result_payload(
            probe,
            action=RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED,
            model_id=model_id,
        )
        await token_mgr.mark_rate_limited(
            token,
            wait_time_seconds=probe.wait_time_seconds,
            probe_result=payload,
            checked_at=probe.checked_at,
        )
        if exhausted_tokens is not None:
            exhausted_tokens.add(token)
        logger.warning(
            "rate_limit_probe_confirmed_exhausted",
            extra={
                "model": model_id,
                "token": f"{token[:10]}...",
                "quota_slot": probe.slot,
                "remaining_queries": probe.remaining_queries,
                "wait_time_seconds": probe.wait_time_seconds,
            },
        )
        return RateLimitResolution(
            action=RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED,
            probe=probe,
        )

    if probe.known:
        payload = _probe_result_payload(
            probe,
            action=RATE_LIMIT_ACTION_RETRY_SAME_TOKEN,
            model_id=model_id,
        )
        await token_mgr.clear_rate_limit_soft_state(
            token,
            probe_result=payload,
            checked_at=probe.checked_at,
        )
        logger.info(
            "rate_limit_probe_cleared_false_positive",
            extra={
                "model": model_id,
                "token": f"{token[:10]}...",
                "quota_slot": probe.slot,
                "remaining_queries": probe.remaining_queries,
                "wait_time_seconds": probe.wait_time_seconds,
            },
        )
        return RateLimitResolution(
            action=RATE_LIMIT_ACTION_RETRY_SAME_TOKEN,
            probe=probe,
            retry_after_seconds=_rate_limit_retry_delay_seconds(),
        )

    payload = _probe_result_payload(
        probe,
        action=RATE_LIMIT_ACTION_SOFT_COOLING,
        model_id=model_id,
    )
    await token_mgr.mark_rate_limited_soft(
        token,
        probe_result=payload,
        checked_at=probe.checked_at,
    )
    logger.warning(
        "rate_limit_probe_unknown_soft_cooled",
        extra={
            "model": model_id,
            "token": f"{token[:10]}...",
            "quota_slot": probe.slot,
            "error": probe.error,
        },
    )
    return RateLimitResolution(
        action=RATE_LIMIT_ACTION_SOFT_COOLING,
        probe=probe,
    )


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
            if hasattr(token_mgr, "is_token_selectable"):
                selectable = token_mgr.is_token_selectable(
                    token_info,
                    pool_name,
                    model_id=model_id,
                )
            else:
                selectable = token_info.is_available(consumed_mode=consumed_mode)
            if not selectable:
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
    candidate_pools = ModelService.pool_candidates_for_model(model_id)
    primary_pool = candidate_pools[0] if candidate_pools else ""
    dedicated_media = ModelService.is_dedicated_media_model(model_id)
    excluded = set(tried or set())
    if exhausted_tokens:
        excluded.update(exhausted_tokens)

    async def _accept(candidate: Optional[str]) -> Optional[str]:
        if not candidate or candidate in excluded:
            return None
        if hasattr(token_mgr, "get_token_entry") and hasattr(token_mgr, "is_token_selectable"):
            pool_name, token_info = token_mgr.get_token_entry(candidate)
            if (
                not token_info
                or not pool_name
                or not token_mgr.is_token_selectable(
                    token_info,
                    pool_name,
                    model_id=model_id,
                )
            ):
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
            preferred_pool = ""
            if hasattr(token_mgr, "get_pool_name_for_token"):
                try:
                    preferred_pool = token_mgr.get_pool_name_for_token(accepted) or ""
                except Exception:
                    preferred_pool = ""
            if hasattr(token_mgr, "bind_token_context"):
                try:
                    token_mgr.bind_token_context(accepted)
                except Exception:
                    pass
            logger.info(
                "token_requirement_selected",
                extra={
                    "model_id": model_id,
                    "candidate_pools": candidate_pools,
                    "selected_pool": preferred_pool,
                    "fallback_from": primary_pool if preferred_pool and preferred_pool != primary_pool else "",
                    "quota_slot": getattr(requirement, "slot", ""),
                    "is_dedicated_media_model": dedicated_media,
                    "token": f"{accepted[:10]}...",
                },
            )
            return TokenSelectionResult(token=accepted, total_candidates=total_candidates)

    for pool_name in candidate_pools:
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
                logger.info(
                    "token_requirement_selected",
                    extra={
                        "model_id": model_id,
                        "candidate_pools": candidate_pools,
                        "selected_pool": pool_name,
                        "fallback_from": primary_pool if pool_name != primary_pool else "",
                        "quota_slot": getattr(requirement, "slot", ""),
                        "is_dedicated_media_model": dedicated_media,
                        "token": f"{accepted[:10]}...",
                    },
                )
                return TokenSelectionResult(
                    token=accepted,
                    total_candidates=total_candidates,
                )
            excluded.add(candidate)

    logger.warning(
        "token_requirement_unavailable",
        extra={
            "model_id": model_id,
            "candidate_pools": candidate_pools,
            "selected_pool": "",
            "fallback_from": "",
            "quota_slot": getattr(requirement, "slot", ""),
            "is_dedicated_media_model": dedicated_media,
            "excluded_count": len(excluded),
        },
    )
    return TokenSelectionResult(token=None, total_candidates=total_candidates)


__all__ = [
    "AUTO_IMAGE_QUOTA_SLOT",
    "DEFAULT_IMAGE_PROBE_TTL_SEC",
    "DEFAULT_TEXT_PROBE_TTL_SEC",
    "IMAGE_LIMIT_ALL_MESSAGE",
    "IMAGE_LIMIT_ERROR_CODE",
    "IMAGE_LIMIT_SINGLE_MESSAGE",
    "IMAGE_QUOTA_SLOT",
    "RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED",
    "RATE_LIMIT_ACTION_RETRY_SAME_TOKEN",
    "RATE_LIMIT_ACTION_SOFT_COOLING",
    "RateLimitResolution",
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
    "rate_limit_requirement_for_model",
    "resolve_rate_limit_hit",
    "select_token_for_requirement",
    "text_auto_quota_requirement",
    "text_grok3_quota_requirement",
    "text_grok4_quota_requirement",
    "video_quota_requirement",
]
