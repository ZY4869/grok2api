import time
import unittest
from unittest.mock import AsyncMock, patch

from app.services.token.models import TokenInfo
from app.services.token.quota import (
    RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED,
    RATE_LIMIT_ACTION_RETRY_SAME_TOKEN,
    RATE_LIMIT_ACTION_SOFT_COOLING,
    QuotaProbeResult,
    resolve_rate_limit_hit,
    text_grok3_quota_requirement,
    text_grok4_quota_requirement,
)


class DummyRateLimitTokenManager:
    def __init__(self):
        self.mark_rate_limited = AsyncMock(return_value=True)
        self.mark_rate_limited_soft = AsyncMock(return_value=True)
        self.clear_rate_limit_soft_state = AsyncMock(return_value=True)


class ResolveRateLimitHitTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_same_token_when_probe_shows_remaining_quota(self):
        token_mgr = DummyRateLimitTokenManager()
        probe = QuotaProbeResult(
            slot="grok-3",
            probe_model="grok-3",
            source_model="grok-3",
            remaining_queries=5,
            wait_time_seconds=None,
            checked_at=123456,
            cache_hit=False,
            exhausted=False,
            known=True,
        )

        with patch(
            "app.services.token.quota.get_config",
            side_effect=lambda key, default=None: {
                "token.rate_limit_probe_on_429_enabled": True,
                "retry.retry_backoff_base": 0.5,
            }.get(key, default),
        ), patch(
            "app.services.token.quota.probe_quota",
            new=AsyncMock(return_value=probe),
        ):
            result = await resolve_rate_limit_hit(
                token_mgr,
                "token-a",
                "grok-3",
                requirement=text_grok3_quota_requirement(),
            )

        self.assertEqual(result.action, RATE_LIMIT_ACTION_RETRY_SAME_TOKEN)
        self.assertEqual(result.retry_after_seconds, 0.5)
        token_mgr.mark_rate_limited.assert_not_awaited()
        token_mgr.mark_rate_limited_soft.assert_not_awaited()
        token_mgr.clear_rate_limit_soft_state.assert_awaited_once()
        payload = token_mgr.clear_rate_limit_soft_state.await_args.kwargs["probe_result"]
        self.assertEqual(payload["action"], RATE_LIMIT_ACTION_RETRY_SAME_TOKEN)
        self.assertEqual(payload["remaining_queries"], 5)

    async def test_confirmed_exhausted_when_probe_returns_wait_window(self):
        token_mgr = DummyRateLimitTokenManager()
        exhausted_tokens = set()
        probe = QuotaProbeResult(
            slot="grok-4",
            probe_model="grok-4",
            source_model="grok-4",
            remaining_queries=0,
            wait_time_seconds=45,
            checked_at=223344,
            cache_hit=False,
            exhausted=True,
            known=True,
        )

        with patch(
            "app.services.token.quota.get_config",
            side_effect=lambda key, default=None: {
                "token.rate_limit_probe_on_429_enabled": True,
            }.get(key, default),
        ), patch(
            "app.services.token.quota.probe_quota",
            new=AsyncMock(return_value=probe),
        ):
            result = await resolve_rate_limit_hit(
                token_mgr,
                "token-b",
                "grok-4",
                requirement=text_grok4_quota_requirement(),
                exhausted_tokens=exhausted_tokens,
            )

        self.assertEqual(result.action, RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED)
        token_mgr.mark_rate_limited.assert_awaited_once()
        kwargs = token_mgr.mark_rate_limited.await_args.kwargs
        self.assertEqual(kwargs["wait_time_seconds"], 45)
        self.assertEqual(kwargs["checked_at"], 223344)
        self.assertIn("token-b", exhausted_tokens)
        token_mgr.mark_rate_limited_soft.assert_not_awaited()
        token_mgr.clear_rate_limit_soft_state.assert_not_awaited()

    async def test_unknown_probe_enters_soft_cooling_instead_of_hard_cooling(self):
        token_mgr = DummyRateLimitTokenManager()
        probe = QuotaProbeResult(
            slot="grok-imagine-1.0-video",
            probe_model="grok-imagine-1.0-video",
            source_model="grok-imagine-1.0-video",
            remaining_queries=None,
            wait_time_seconds=None,
            checked_at=998877,
            cache_hit=False,
            exhausted=False,
            known=False,
            error="cloudflare blocked",
        )

        with patch(
            "app.services.token.quota.get_config",
            side_effect=lambda key, default=None: {
                "token.rate_limit_probe_on_429_enabled": True,
            }.get(key, default),
        ), patch(
            "app.services.token.quota.probe_quota",
            new=AsyncMock(return_value=probe),
        ):
            result = await resolve_rate_limit_hit(
                token_mgr,
                "token-c",
                "grok-imagine-1.0-video",
            )

        self.assertEqual(result.action, RATE_LIMIT_ACTION_SOFT_COOLING)
        token_mgr.mark_rate_limited.assert_not_awaited()
        token_mgr.clear_rate_limit_soft_state.assert_not_awaited()
        token_mgr.mark_rate_limited_soft.assert_awaited_once()
        payload = token_mgr.mark_rate_limited_soft.await_args.kwargs["probe_result"]
        self.assertEqual(payload["action"], RATE_LIMIT_ACTION_SOFT_COOLING)
        self.assertEqual(payload["error"], "cloudflare blocked")


class TokenInfoSoftCoolingTests(unittest.TestCase):
    def test_soft_cooling_makes_active_token_temporarily_unavailable(self):
        token = TokenInfo(token="token-soft", quota=12)
        token.set_soft_rate_limit(int(time.time() * 1000) + 30_000)

        self.assertFalse(token.is_available())

        token.clear_soft_rate_limit()
        self.assertTrue(token.is_available())

    def test_need_refresh_honors_explicit_cooling_until(self):
        token = TokenInfo(token="token-cooling", quota=0)
        token.enter_cooling(until_ms=int(time.time() * 1000) - 1000)

        self.assertTrue(token.need_refresh(interval_hours=8))


if __name__ == "__main__":
    unittest.main()
