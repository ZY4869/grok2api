"""Helpers for ssoHeavy capability-scoped quota behavior."""

from __future__ import annotations

from typing import Any, Optional

from app.core.config import get_config
from app.services.grok.services.model import HEAVY_POOL_NAME
from app.services.token.quota import QuotaRequirement, rate_limit_requirement_for_model

HEAVY_DEFAULT_LOCAL_QUOTA = 0


def is_upstream_quota_pool(pool_name: str) -> bool:
    return str(pool_name or "").strip() == HEAVY_POOL_NAME


def default_local_quota_for_pool(
    pool_name: str,
    *,
    basic_default: int,
    super_default: int,
) -> int:
    if is_upstream_quota_pool(pool_name):
        return HEAVY_DEFAULT_LOCAL_QUOTA
    if str(pool_name or "").strip() == "ssoSuper":
        return super_default
    return basic_default


def capability_requirement_for_model(model_id: Optional[str]) -> Optional[QuotaRequirement]:
    return rate_limit_requirement_for_model(str(model_id or "").strip())


def _quota_cache_ttl_ms(requirement: QuotaRequirement) -> int:
    value = get_config(requirement.ttl_config_key, requirement.ttl_default_sec)
    try:
        seconds = max(0.0, float(value))
    except Exception:
        seconds = float(requirement.ttl_default_sec)
    return int(seconds * 1000)


def _read_int(payload: dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = payload.get(key)
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            continue
    return None


def capability_wait_active(
    rate_limits: Any,
    requirement: Optional[QuotaRequirement],
    *,
    now_ms: int,
) -> bool:
    if not requirement or not isinstance(rate_limits, dict):
        return False

    entry = rate_limits.get(requirement.slot)
    if not isinstance(entry, dict):
        return False

    checked_at = _read_int(entry, "checkedAt", "checked_at") or 0
    if checked_at > 0 and now_ms - checked_at > _quota_cache_ttl_ms(requirement):
        return False

    remaining = _read_int(
        entry,
        "remainingQueries",
        "remaining_queries",
        "remainingTokens",
        "remaining_tokens",
    )
    wait_seconds = _read_int(entry, "waitTimeSeconds", "wait_time_seconds")

    known = remaining is not None or wait_seconds is not None
    exhausted = (
        known
        and (
            (remaining is not None and remaining <= 0)
            or (wait_seconds is not None and wait_seconds > 0)
        )
    )
    if not exhausted:
        return False

    if wait_seconds is not None and wait_seconds > 0 and checked_at > 0:
        return now_ms < checked_at + wait_seconds * 1000

    return True

