"""
Batch refresh service for real account quota.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from app.core.batch import run_batch
from app.core.config import get_config
from app.core.logger import logger
from app.services.reverse.rate_limits import RateLimitsReverse
from app.services.reverse.refresh_x_subscription_status import (
    RefreshXSubscriptionStatusReverse,
)
from app.services.reverse.subscriptions import SubscriptionsReverse
from app.services.reverse.utils.session import ResettableSession

REAL_QUOTA_TARGETS = (
    {
        "slot": "grok-3",
        "queries": (
            {"model_name": "grok-3"},
        ),
    },
    {
        "slot": "grok-4",
        "queries": (
            {"model_name": "grok-4"},
        ),
    },
    {
        "slot": "grok-imagine-1.0",
        "queries": (
            {"model_name": "grok-imagine-1.0"},
            {"model_name": "grok-imagine-1.0-fast"},
            {"model_name": "grok-imagine-1.0-edit"},
        ),
    },
    {
        "slot": "grok-imagine-1.0-video",
        "queries": (
            {"model_name": "grok-imagine-1.0-video"},
        ),
    },
)
REAL_QUOTA_MODELS = tuple(target["slot"] for target in REAL_QUOTA_TARGETS)
REAL_QUOTA_MODEL_LABELS = {
    "grok-3": "text (grok-3)",
    "grok-4": "text (grok-4)",
    "grok-imagine-1.0": "image",
    "grok-imagine-1.0-video": "video",
}
ACTIVE_SUBSCRIPTION_STATUSES = {"ACTIVE", "SUBSCRIPTION_STATUS_ACTIVE"}
INVALID_TIER = "SUBSCRIPTION_TIER_INVALID"
TIER_LABELS = {
    INVALID_TIER: "Free",
    "SUBSCRIPTION_TIER_X_BASIC": "Basic",
    "SUBSCRIPTION_TIER_X_PREMIUM": "Premium",
    "SUBSCRIPTION_TIER_X_PREMIUM_PLUS": "PremiumPlus",
    "SUBSCRIPTION_TIER_GROK_PRO": "SuperGrok",
    "SUBSCRIPTION_TIER_SUPER_GROK_PRO": "SuperGrokPro",
}
TIER_PRIORITY = {
    INVALID_TIER: 0,
    "SUBSCRIPTION_TIER_X_BASIC": 1,
    "SUBSCRIPTION_TIER_X_PREMIUM": 2,
    "SUBSCRIPTION_TIER_X_PREMIUM_PLUS": 3,
    "SUBSCRIPTION_TIER_GROK_PRO": 4,
    "SUBSCRIPTION_TIER_SUPER_GROK_PRO": 5,
}

_REAL_QUOTA_SEMAPHORE = None
_REAL_QUOTA_SEM_VALUE = None


def _get_real_quota_semaphore() -> asyncio.Semaphore:
    try:
        value = max(1, int(get_config("usage.concurrent") or 1))
    except Exception:
        value = 1

    global _REAL_QUOTA_SEMAPHORE, _REAL_QUOTA_SEM_VALUE
    if _REAL_QUOTA_SEMAPHORE is None or value != _REAL_QUOTA_SEM_VALUE:
        _REAL_QUOTA_SEM_VALUE = value
        _REAL_QUOTA_SEMAPHORE = asyncio.Semaphore(value)
    return _REAL_QUOTA_SEMAPHORE


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return {}


def _extract_subscription_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("subscriptions", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _subscription_provider(item: dict[str, Any]) -> str:
    for key in ("stripe", "paypal", "braintree", "enterprise", "adhoc", "eapi", "x"):
        value = item.get(key)
        if isinstance(value, dict) and value:
            return key
    return "unknown"


def _normalize_subscription(item: dict[str, Any]) -> dict[str, Any]:
    tier = str(item.get("tier") or INVALID_TIER)
    normalized = {
        "tier": tier,
        "tier_name": TIER_LABELS.get(tier, tier.replace("SUBSCRIPTION_TIER_", "")),
        "status": str(item.get("status") or ""),
        "provider": _subscription_provider(item),
    }

    stripe = item.get("stripe")
    if isinstance(stripe, dict):
        if stripe.get("subscriptionType"):
            normalized["subscription_type"] = str(stripe.get("subscriptionType"))
        if stripe.get("subscriptionName"):
            normalized["subscription_name"] = str(stripe.get("subscriptionName"))
        if stripe.get("planName"):
            normalized["plan_name"] = str(stripe.get("planName"))
        if stripe.get("productName"):
            normalized["product_name"] = str(stripe.get("productName"))
        if stripe.get("currentPeriodEnd") is not None:
            normalized["current_period_end"] = stripe.get("currentPeriodEnd")
        if stripe.get("cancelAtPeriodEnd") is not None:
            normalized["cancel_at_period_end"] = bool(stripe.get("cancelAtPeriodEnd"))

    for source_key, target_key in (
        ("name", "name"),
        ("displayName", "display_name"),
        ("subscriptionName", "subscription_name"),
        ("planName", "plan_name"),
        ("productName", "product_name"),
    ):
        value = item.get(source_key)
        if value:
            normalized[target_key] = str(value)

    enterprise = item.get("enterprise")
    if isinstance(enterprise, dict) and enterprise.get("teamId"):
        normalized["team_id"] = str(enterprise.get("teamId"))

    return normalized


def _is_active_subscription(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").upper()
    return status in ACTIVE_SUBSCRIPTION_STATUSES or status.endswith("_ACTIVE")


def _pick_real_tier(subscriptions: list[dict[str, Any]]) -> tuple[str, str]:
    tier = INVALID_TIER
    for item in subscriptions:
        current = str(item.get("tier") or INVALID_TIER)
        if TIER_PRIORITY.get(current, 0) > TIER_PRIORITY.get(tier, 0):
            tier = current
    return tier, TIER_LABELS.get(tier, "Free")


def _normalize_rate_limit(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    normalized: dict[str, Any] = {}
    for key in (
        "windowSizeSeconds",
        "waitTimeSeconds",
        "totalQueries",
        "remainingQueries",
        "totalTokens",
        "remainingTokens",
    ):
        if key in payload:
            normalized[key] = payload.get(key)

    for key in ("lowEffortRateLimits", "highEffortRateLimits"):
        if isinstance(payload.get(key), dict):
            normalized[key] = _normalize_rate_limit(payload.get(key))

    return normalized


def _has_rate_limit_data(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    for key in (
        "waitTimeSeconds",
        "totalQueries",
        "remainingQueries",
        "totalTokens",
        "remainingTokens",
    ):
        if payload.get(key) is not None:
            return True

    return any(
        _has_rate_limit_data(payload.get(key))
        for key in ("lowEffortRateLimits", "highEffortRateLimits")
        if isinstance(payload.get(key), dict)
    )


def _rate_limit_query_label(query: dict[str, Any]) -> str:
    model_name = str(query.get("model_name") or "")
    request_kind = str(query.get("request_kind") or "DEFAULT")
    if request_kind and request_kind != "DEFAULT":
        return f"{model_name}[{request_kind}]"
    return model_name


async def _fetch_rate_limit_for_target(
    session: Any,
    token: str,
    target: dict[str, Any],
) -> tuple[dict[str, Any], bool, str]:
    slot = str(target.get("slot") or "")
    queries = target.get("queries") or ()
    candidate_errors: list[str] = []

    for query in queries:
        model_name = str(query.get("model_name") or "")
        request_kind = str(query.get("request_kind") or "DEFAULT")
        query_label = _rate_limit_query_label(query)

        try:
            response = await RateLimitsReverse.request(
                session,
                token,
                model_name=model_name,
                request_kind=request_kind,
            )
            normalized = _normalize_rate_limit(_safe_json(response))
            if not _has_rate_limit_data(normalized):
                candidate_errors.append(f"{query_label}: no quota data returned")
                logger.warning(
                    "Real quota refresh: slot={} query={} token={} returned no quota data",
                    slot,
                    query_label,
                    f"{token[:10]}...",
                )
                continue

            if model_name != slot:
                normalized["sourceModelName"] = model_name
            if request_kind != "DEFAULT":
                normalized["sourceRequestKind"] = request_kind
            return normalized, True, ""
        except Exception as exc:
            error_text = str(exc)
            candidate_errors.append(f"{query_label}: {error_text}")
            logger.warning(
                "Real quota refresh: slot={} query={} token={} failed: {}",
                slot,
                query_label,
                f"{token[:10]}...",
                error_text,
            )

    error_text = "; ".join(candidate_errors) if candidate_errors else "No quota data returned"
    return {"error": error_text}, False, f"{slot}: {error_text}"


def _describe_rate_limit(model_name: str, payload: dict[str, Any]) -> str:
    display_name = REAL_QUOTA_MODEL_LABELS.get(model_name, model_name)
    if not isinstance(payload, dict):
        return f"{display_name}: -"
    if payload.get("error"):
        return f"{display_name}: failed"

    remaining = payload.get("remainingTokens")
    total = payload.get("totalTokens")
    if remaining is None:
        remaining = payload.get("remainingQueries")
    if total is None:
        total = payload.get("totalQueries")

    if remaining is not None and total is not None:
        return f"{display_name}: {remaining}/{total}"
    if remaining is not None:
        return f"{display_name}: {remaining}"

    wait = payload.get("waitTimeSeconds")
    if wait:
        return f"{display_name}: wait {wait}s"

    return f"{display_name}: -"


def _build_summary(rate_limits: dict[str, Any]) -> str:
    parts = [
        _describe_rate_limit(model_name, rate_limits.get(model_name) or {})
        for model_name in REAL_QUOTA_MODELS
    ]
    return " | ".join(parts)


def _find_token_entry(mgr, token: str):
    raw_token = token[4:] if token.startswith("sso=") else token
    for pool_name, pool in mgr.pools.items():
        token_info = pool.get(raw_token)
        if token_info:
            return pool_name, token_info
    return None, None


def _apply_snapshot(
    mgr, token: str, snapshot: Optional[dict[str, Any]], error: str = ""
):
    pool_name, token_info = _find_token_entry(mgr, token)
    if not token_info:
        raise ValueError("Token not found")

    token_info.last_real_quota_check_at = int(datetime.now().timestamp() * 1000)
    if snapshot is not None:
        token_info.real_tier = snapshot.get("subscription_tier")
        token_info.real_tier_name = snapshot.get("subscription_name")
        token_info.real_quota = snapshot
    token_info.last_real_quota_error = error or None
    mgr._track_token_change(token_info, pool_name, "state")


class RealQuotaRefreshService:
    """Refresh real subscription tier and live rate limits for tokens."""

    async def refresh(self, token: str, mgr) -> dict[str, Any]:
        pool_name, _ = _find_token_entry(mgr, token)
        if not pool_name:
            raise ValueError("Token not found")

        raw_token = token[4:] if token.startswith("sso=") else token

        try:
            partial_errors = []
            subscription_payload: Any = {}
            rate_limits: dict[str, Any] = {}
            success_models = 0

            async with _get_real_quota_semaphore():
                async with ResettableSession() as session:
                    # This endpoint appears to be optional in practice. Some accounts
                    # return 501, but subscriptions/rate-limits can still be queried.
                    try:
                        await RefreshXSubscriptionStatusReverse.request(
                            session, raw_token
                        )
                    except Exception as exc:
                        error_text = str(exc)
                        partial_errors.append(
                            f"refresh-subscription: {error_text}"
                        )
                        logger.warning(
                            "Real quota refresh: token={} refresh-x-subscription-status failed: {}",
                            f"{raw_token[:10]}...",
                            error_text,
                        )

                    try:
                        subscriptions_response = await SubscriptionsReverse.request(
                            session, raw_token
                        )
                        subscription_payload = _safe_json(subscriptions_response)
                    except Exception as exc:
                        error_text = str(exc)
                        partial_errors.append(f"subscriptions: {error_text}")
                        logger.warning(
                            "Real quota refresh: token={} subscriptions failed: {}",
                            f"{raw_token[:10]}...",
                            error_text,
                        )

                    for target in REAL_QUOTA_TARGETS:
                        slot = str(target.get("slot") or "")
                        payload, ok, partial_error = await _fetch_rate_limit_for_target(
                            session,
                            raw_token,
                            target,
                        )
                        rate_limits[slot] = payload
                        if ok:
                            success_models += 1
                        elif partial_error:
                            partial_errors.append(partial_error)

            subscriptions = [
                _normalize_subscription(item)
                for item in _extract_subscription_items(subscription_payload)
            ]
            active_subscriptions = [
                item for item in subscriptions if _is_active_subscription(item)
            ]
            tier, tier_name = _pick_real_tier(active_subscriptions)

            snapshot = {
                "subscription_tier": tier,
                "subscription_name": tier_name,
                "subscriptions": subscriptions,
                "active_subscriptions": active_subscriptions,
                "rate_limits": rate_limits,
                "refresh_ok": success_models > 0,
                "real_quota_summary": _build_summary(rate_limits),
            }
            if partial_errors:
                snapshot["partial_errors"] = partial_errors

            error_text = ""
            if success_models == 0:
                error_text = "No live model quota returned"

            _apply_snapshot(mgr, raw_token, snapshot, error_text)
            return snapshot

        except Exception as exc:
            _apply_snapshot(mgr, raw_token, None, str(exc))
            raise

    async def batch(
        self,
        tokens: list[str],
        mgr,
        *,
        on_item: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        batch_size = get_config("usage.batch_size")

        async def _refresh_one(item: str):
            return await self.refresh(item, mgr)

        return await run_batch(
            tokens,
            _refresh_one,
            batch_size=batch_size,
            on_item=on_item,
            should_cancel=should_cancel,
        )


__all__ = [
    "REAL_QUOTA_MODELS",
    "RealQuotaRefreshService",
    "_build_summary",
    "_normalize_rate_limit",
    "_normalize_subscription",
    "_pick_real_tier",
]
