"""
Model-specific entitlement helpers.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions import AppException, ErrorType
from app.services.grok.services.model import HEAVY_POOL_NAME, SUPER_POOL_NAME
from app.services.token.models import TokenInfo

HEAVY_MODEL_ID = "grok-4-heavy"
SUPER_TEXT_MODELS = frozenset({"grok-auto", "grok-4-expert"})
FREE_TEXT_MODELS = frozenset({"grok-3-fast"})
HEAVY_TEXT_MODELS = frozenset({HEAVY_MODEL_ID})

SUPER_ACCESS_ERROR_CODE = "insufficient_super_subscription"
SUPER_ACCESS_ERROR_MESSAGE = (
    "This model requires a token assigned to the ssoSuper or ssoHeavy pool."
)
HEAVY_ACCESS_ERROR_CODE = "insufficient_heavy_subscription"
HEAVY_ACCESS_ERROR_MESSAGE = (
    "grok-4-heavy requires a token assigned to the ssoHeavy pool."
)

FREE_ACCESS = "free"
SUPER_ACCESS = "super"
HEAVY_ACCESS = "heavy"
UNKNOWN_ACCESS = "unknown"

FREE_TIERS = frozenset(
    {
        "SUBSCRIPTION_TIER_INVALID",
        "SUBSCRIPTION_TIER_X_BASIC",
        "SUBSCRIPTION_TIER_X_PREMIUM",
        "SUBSCRIPTION_TIER_X_PREMIUM_PLUS",
    }
)
SUPER_TIERS = frozenset({"SUBSCRIPTION_TIER_GROK_PRO"})
HEAVY_TIERS = frozenset({"SUBSCRIPTION_TIER_SUPER_GROK_PRO"})


def required_access_for_model(model_id: str) -> str:
    model = str(model_id or "").strip()
    if model in HEAVY_TEXT_MODELS:
        return HEAVY_ACCESS
    if model in SUPER_TEXT_MODELS:
        return SUPER_ACCESS
    if model in FREE_TEXT_MODELS:
        return FREE_ACCESS
    return UNKNOWN_ACCESS


def model_requires_special_subscription(model_id: str) -> bool:
    return required_access_for_model(model_id) in {SUPER_ACCESS, HEAVY_ACCESS}


def _normalize_tier(value: Any) -> str:
    return str(value or "").strip()


def _pool_grants_required_access(pool_name: str, required_access: str) -> bool:
    pool = str(pool_name or "").strip()
    if required_access == HEAVY_ACCESS:
        return pool == HEAVY_POOL_NAME
    if required_access == SUPER_ACCESS:
        return pool in {SUPER_POOL_NAME, HEAVY_POOL_NAME}
    if required_access == FREE_ACCESS:
        return pool in {"ssoBasic", SUPER_POOL_NAME, HEAVY_POOL_NAME}
    return True


def _tier_to_access(tier: str) -> str:
    normalized = _normalize_tier(tier)
    if normalized in HEAVY_TIERS:
        return HEAVY_ACCESS
    if normalized in SUPER_TIERS:
        return SUPER_ACCESS
    if normalized in FREE_TIERS:
        return FREE_ACCESS
    if normalized:
        return FREE_ACCESS
    return UNKNOWN_ACCESS


def token_text_access_state(token: Optional[TokenInfo]) -> str:
    if token is None:
        return UNKNOWN_ACCESS

    quota = token.real_quota if isinstance(token.real_quota, dict) else {}
    active_subscriptions = quota.get("active_subscriptions")
    if not isinstance(active_subscriptions, list) or not active_subscriptions:
        direct_tier = _normalize_tier(token.real_tier or quota.get("subscription_tier"))
        return _tier_to_access(direct_tier)

    highest = FREE_ACCESS
    for item in active_subscriptions:
        if not isinstance(item, dict):
            continue
        current = _tier_to_access(item.get("tier"))
        if current == HEAVY_ACCESS:
            return HEAVY_ACCESS
        if current == SUPER_ACCESS:
            highest = SUPER_ACCESS
    return highest


def _known_access_state(access_state: str) -> bool:
    return access_state in {FREE_ACCESS, SUPER_ACCESS, HEAVY_ACCESS}


def _access_satisfies(required_access: str, access_state: str) -> bool:
    if required_access == FREE_ACCESS:
        return access_state in {FREE_ACCESS, SUPER_ACCESS, HEAVY_ACCESS}
    if required_access == SUPER_ACCESS:
        return access_state in {SUPER_ACCESS, HEAVY_ACCESS}
    if required_access == HEAVY_ACCESS:
        return access_state == HEAVY_ACCESS
    return True


def token_supports_model(
    token: Optional[TokenInfo],
    model_id: str,
    *,
    pool_name: Optional[str] = None,
) -> bool:
    required_access = required_access_for_model(model_id)
    if required_access == UNKNOWN_ACCESS:
        return True
    if token is None:
        return False

    if pool_name and not _pool_grants_required_access(pool_name, required_access):
        return False

    access_state = token_text_access_state(token)
    if not _known_access_state(access_state):
        return True
    return _access_satisfies(required_access, access_state)


def token_supports_model_with_reason(
    token: Optional[TokenInfo],
    model_id: str,
    *,
    pool_name: Optional[str] = None,
) -> tuple[bool, str]:
    required_access = required_access_for_model(model_id)
    if required_access == UNKNOWN_ACCESS:
        return True, ""
    if token is None:
        return False, "missing_token"

    if pool_name and not _pool_grants_required_access(pool_name, required_access):
        if required_access == HEAVY_ACCESS:
            return False, "pool_mismatch_heavy"
        if required_access == SUPER_ACCESS:
            return False, "pool_mismatch_super"
        return False, "pool_mismatch"

    access_state = token_text_access_state(token)
    if not _known_access_state(access_state):
        return True, "unknown_real_tier_pool_fallback"
    if _access_satisfies(required_access, access_state):
        return True, ""
    if required_access == HEAVY_ACCESS:
        return False, "real_tier_lacks_heavy"
    if required_access == SUPER_ACCESS:
        return False, "real_tier_lacks_super"
    return False, "real_tier_denied"


def model_access_denied_error(model_id: str) -> AppException:
    required_access = required_access_for_model(model_id)
    if required_access == HEAVY_ACCESS:
        return AppException(
            message=HEAVY_ACCESS_ERROR_MESSAGE,
            error_type=ErrorType.PERMISSION.value,
            code=HEAVY_ACCESS_ERROR_CODE,
            param="model",
            status_code=403,
        )
    if required_access == SUPER_ACCESS:
        return AppException(
            message=SUPER_ACCESS_ERROR_MESSAGE,
            error_type=ErrorType.PERMISSION.value,
            code=SUPER_ACCESS_ERROR_CODE,
            param="model",
            status_code=403,
        )
    return AppException(
        message=f"{model_id} is not available for the current account.",
        error_type=ErrorType.PERMISSION.value,
        code="model_access_denied",
        param="model",
        status_code=403,
    )


__all__ = [
    "FREE_ACCESS",
    "FREE_TEXT_MODELS",
    "HEAVY_ACCESS",
    "HEAVY_ACCESS_ERROR_CODE",
    "HEAVY_ACCESS_ERROR_MESSAGE",
    "HEAVY_MODEL_ID",
    "HEAVY_TEXT_MODELS",
    "SUPER_ACCESS",
    "SUPER_ACCESS_ERROR_CODE",
    "SUPER_ACCESS_ERROR_MESSAGE",
    "SUPER_TEXT_MODELS",
    "model_access_denied_error",
    "model_requires_special_subscription",
    "required_access_for_model",
    "token_supports_model",
    "token_supports_model_with_reason",
    "token_text_access_state",
]
