import unittest
from unittest.mock import AsyncMock, patch

from app.services.token.models import TokenInfo
from app.services.token.real_quota import RealQuotaRefreshService

from app.services.token.real_quota import (
    _build_summary,
    _normalize_rate_limit,
    _normalize_subscription,
    _pick_real_tier,
)


class RealQuotaHelperTests(unittest.TestCase):
    def test_pick_real_tier_prefers_highest_active_subscription(self):
        subscriptions = [
            _normalize_subscription(
                {
                    "tier": "SUBSCRIPTION_TIER_X_BASIC",
                    "status": "SUBSCRIPTION_STATUS_ACTIVE",
                }
            ),
            _normalize_subscription(
                {
                    "tier": "SUBSCRIPTION_TIER_GROK_PRO",
                    "status": "SUBSCRIPTION_STATUS_ACTIVE",
                    "stripe": {
                        "subscriptionType": "monthly",
                        "currentPeriodEnd": 1711111111,
                    },
                }
            ),
        ]

        tier, label = _pick_real_tier(subscriptions)

        self.assertEqual(tier, "SUBSCRIPTION_TIER_GROK_PRO")
        self.assertEqual(label, "SuperGrok")
        self.assertEqual(subscriptions[1]["subscription_type"], "monthly")
        self.assertEqual(subscriptions[1]["current_period_end"], 1711111111)

    def test_build_summary_uses_tokens_and_queries(self):
        summary = _build_summary(
            {
                "grok-3": _normalize_rate_limit(
                    {
                        "remainingTokens": 120,
                        "totalTokens": 1000,
                        "waitTimeSeconds": 0,
                    }
                ),
                "grok-4": _normalize_rate_limit(
                    {
                        "remainingQueries": 6,
                        "totalQueries": 10,
                        "waitTimeSeconds": 30,
                    }
                ),
                "grok-imagine-1.0": _normalize_rate_limit(
                    {
                        "remainingQueries": 8,
                        "totalQueries": 20,
                    }
                ),
                "grok-imagine-1.0-video": _normalize_rate_limit(
                    {
                        "remainingQueries": 2,
                        "totalQueries": 5,
                    }
                ),
            }
        )

        self.assertIn("text (grok-3): 120/1000", summary)
        self.assertIn("text (grok-4): 6/10", summary)
        self.assertIn("image: 8/20", summary)
        self.assertIn("video: 2/5", summary)


class _DummyPool:
    def __init__(self, token_info):
        self._token_info = token_info

    def get(self, token):
        if token == self._token_info.token:
            return self._token_info
        return None


class _DummyMgr:
    def __init__(self, token_info):
        self.pools = {"ssoBasic": _DummyPool(token_info)}
        self.tracked = []

    def _track_token_change(self, token, pool_name, change_kind):
        self.tracked.append((token.token, pool_name, change_kind))


class _DummySession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class RealQuotaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_continues_when_refresh_subscription_returns_501(self):
        token_info = TokenInfo(token="token-1")
        mgr = _DummyMgr(token_info)
        service = RealQuotaRefreshService()

        async def _rate_limit_request(session, token, *, model_name="grok-3", request_kind="DEFAULT"):
            if model_name == "grok-3":
                return _DummyResponse({"remainingTokens": 120, "totalTokens": 1000})
            if model_name == "grok-4":
                return _DummyResponse({"remainingQueries": 6, "totalQueries": 10})
            if model_name == "grok-imagine-1.0":
                return _DummyResponse({"remainingQueries": 8, "totalQueries": 20})
            if model_name == "grok-imagine-1.0-video":
                return _DummyResponse({"remainingQueries": 2, "totalQueries": 5})
            raise AssertionError(f"unexpected model: {model_name}")

        with (
            patch("app.services.token.real_quota.get_config", return_value=1),
            patch(
                "app.services.token.real_quota.ResettableSession",
                return_value=_DummySession(),
            ),
            patch(
                "app.services.token.real_quota.RefreshXSubscriptionStatusReverse.request",
                new=AsyncMock(side_effect=Exception("Request failed, 501")),
            ),
            patch(
                "app.services.token.real_quota.SubscriptionsReverse.request",
                new=AsyncMock(
                    return_value=_DummyResponse(
                        [
                            {
                                "tier": "SUBSCRIPTION_TIER_GROK_PRO",
                                "status": "SUBSCRIPTION_STATUS_ACTIVE",
                            }
                        ]
                    )
                ),
            ),
            patch(
                "app.services.token.real_quota.RateLimitsReverse.request",
                new=AsyncMock(side_effect=_rate_limit_request),
            ),
        ):
            result = await service.refresh("token-1", mgr)

        self.assertTrue(result["refresh_ok"])
        self.assertEqual(result["subscription_tier"], "SUBSCRIPTION_TIER_GROK_PRO")
        self.assertEqual(result["subscription_name"], "SuperGrok")
        self.assertEqual(result["rate_limits"]["grok-3"]["remainingTokens"], 120)
        self.assertEqual(result["rate_limits"]["grok-imagine-1.0"]["remainingQueries"], 8)
        self.assertEqual(result["rate_limits"]["grok-imagine-1.0-video"]["remainingQueries"], 2)
        self.assertIn("image: 8/20", result["real_quota_summary"])
        self.assertIn("video: 2/5", result["real_quota_summary"])
        self.assertIn("refresh-subscription: Request failed, 501", result["partial_errors"])
        self.assertIsNone(token_info.last_real_quota_error)
        self.assertEqual(token_info.real_tier_name, "SuperGrok")
        self.assertEqual(mgr.tracked[-1], ("token-1", "ssoBasic", "state"))


class _DummyPool:
    def __init__(self, token_info):
        self._token_info = token_info

    def get(self, token):
        if token == self._token_info.token:
            return self._token_info
        return None


class _DummyMgr:
    def __init__(self, token_info):
        self.pools = {"ssoBasic": _DummyPool(token_info)}
        self.tracked = []

    def _track_token_change(self, token, pool_name, change_kind):
        self.tracked.append((token.token, pool_name, change_kind))


class _DummySession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class RealQuotaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_continues_when_refresh_subscription_returns_501(self):
        token_info = TokenInfo(token="token-1")
        mgr = _DummyMgr(token_info)
        service = RealQuotaRefreshService()

        async def _rate_limit_request(session, token, *, model_name="grok-3", request_kind="DEFAULT"):
            if model_name == "grok-3":
                return _DummyResponse({"remainingTokens": 120, "totalTokens": 1000})
            return _DummyResponse({"remainingQueries": 6, "totalQueries": 10})

        with (
            patch("app.services.token.real_quota.get_config", return_value=1),
            patch(
                "app.services.token.real_quota.ResettableSession",
                return_value=_DummySession(),
            ),
            patch(
                "app.services.token.real_quota.RefreshXSubscriptionStatusReverse.request",
                new=AsyncMock(side_effect=Exception("Request failed, 501")),
            ),
            patch(
                "app.services.token.real_quota.SubscriptionsReverse.request",
                new=AsyncMock(
                    return_value=_DummyResponse(
                        [
                            {
                                "tier": "SUBSCRIPTION_TIER_GROK_PRO",
                                "status": "SUBSCRIPTION_STATUS_ACTIVE",
                            }
                        ]
                    )
                ),
            ),
            patch(
                "app.services.token.real_quota.RateLimitsReverse.request",
                new=AsyncMock(side_effect=_rate_limit_request),
            ),
        ):
            result = await service.refresh("token-1", mgr)

        self.assertTrue(result["refresh_ok"])
        self.assertEqual(result["subscription_tier"], "SUBSCRIPTION_TIER_GROK_PRO")
        self.assertEqual(result["subscription_name"], "SuperGrok")
        self.assertEqual(result["rate_limits"]["grok-3"]["remainingTokens"], 120)
        self.assertIn("refresh-subscription: Request failed, 501", result["partial_errors"])
        self.assertIsNone(token_info.last_real_quota_error)
        self.assertEqual(token_info.real_tier_name, "SuperGrok")
        self.assertEqual(mgr.tracked[-1], ("token-1", "ssoBasic", "state"))


if __name__ == "__main__":
    unittest.main()
