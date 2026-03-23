"""
Model-specific entitlement helpers.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions import AppException, ErrorType
from app.services.token.models import TokenInfo

HEAVY_MODEL_ID = "grok-4-heavy"
HEAVY_ACCESS_ERROR_CODE = "insufficient_heavy_subscription"
HEAVY_ACCESS_ERROR_MESSAGE = (
    "grok-4-heavy requires a token assigned to the ssoHeavy pool."
)

_HEAVY_MARKER = "heavy"
_HEAVY_CANDIDATE_FIELDS = (
    "subscription_type",
    "subscription_name",
    "plan_name",
    "product_name",
    "display_name",
    "name",
    "tier_name",
    "tier",
)


def model_requires_special_subscription(model_id: str) -> bool:
    return str(model_id or "").strip() == HEAVY_MODEL_ID


def _contains_heavy_marker(value: Any) -> bool:
    return isinstance(value, str) and _HEAVY_MARKER in value.strip().lower()


def subscription_grants_heavy_access(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return any(_contains_heavy_marker(item.get(key)) for key in _HEAVY_CANDIDATE_FIELDS)


def token_heavy_access_state(token: Optional[TokenInfo]) -> str:
    if token is None:
        return "unknown"

    quota = token.real_quota if isinstance(token.real_quota, dict) else {}
    active_subscriptions = quota.get("active_subscriptions")
    if not isinstance(active_subscriptions, list) or not active_subscriptions:
        return "unknown"

    if any(subscription_grants_heavy_access(item) for item in active_subscriptions):
        return "granted"
    return "denied"


def token_supports_model(token: Optional[TokenInfo], model_id: str) -> bool:
    if not model_requires_special_subscription(model_id):
        return True
    return token_heavy_access_state(token) == "granted"


def model_access_denied_error(model_id: str) -> AppException:
    if model_requires_special_subscription(model_id):
        return AppException(
            message=HEAVY_ACCESS_ERROR_MESSAGE,
            error_type=ErrorType.PERMISSION.value,
            code=HEAVY_ACCESS_ERROR_CODE,
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
    "HEAVY_ACCESS_ERROR_CODE",
    "HEAVY_ACCESS_ERROR_MESSAGE",
    "HEAVY_MODEL_ID",
    "model_access_denied_error",
    "model_requires_special_subscription",
    "subscription_grants_heavy_access",
    "token_heavy_access_state",
    "token_supports_model",
]
