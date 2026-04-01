import unittest
from unittest.mock import AsyncMock, patch

from app.services.grok.services.model import HEAVY_POOL_NAME
from app.services.token.manager import TokenManager
from app.services.token.models import EffortType, TokenInfo, TokenStatus
from app.services.token.pool import TokenPool


def _build_manager(*, heavy_tokens=None, basic_tokens=None, super_tokens=None) -> TokenManager:
    mgr = TokenManager()
    mgr.initialized = True
    mgr.pools = {}

    basic_pool = TokenPool("ssoBasic")
    for token in basic_tokens or []:
        basic_pool.add(token)
    mgr.pools["ssoBasic"] = basic_pool

    super_pool = TokenPool("ssoSuper")
    for token in super_tokens or []:
        super_pool.add(token)
    mgr.pools["ssoSuper"] = super_pool

    heavy_pool = TokenPool(HEAVY_POOL_NAME)
    for token in heavy_tokens or []:
        heavy_pool.add(token)
    mgr.pools[HEAVY_POOL_NAME] = heavy_pool
    return mgr


class HeavyQuotaIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_heavy_rate_limit_marks_only_current_slot(self):
        token = TokenInfo(token="token-heavy", quota=17)
        mgr = _build_manager(heavy_tokens=[token])
        checked_at = mgr._now_ms()

        marked = await mgr.mark_rate_limited(
            "token-heavy",
            wait_time_seconds=60,
            probe_result={
                "slot": "grok-imagine-1.0",
                "remaining_queries": 0,
                "wait_time_seconds": 60,
            },
            checked_at=checked_at,
        )

        self.assertTrue(marked)
        self.assertEqual(token.status, TokenStatus.ACTIVE)
        self.assertEqual(token.quota, 17)
        self.assertIsNone(token.cooling_until)
        self.assertIsNone(
            mgr.get_token_info(HEAVY_POOL_NAME, model_id="grok-imagine-1.0")
        )
        self.assertIsNotNone(
            mgr.get_token_info(HEAVY_POOL_NAME, model_id="grok-4-expert")
        )
        self.assertIsNotNone(
            mgr.get_token_info(HEAVY_POOL_NAME, model_id="grok-imagine-1.0-video")
        )

    async def test_heavy_consume_updates_usage_without_spending_local_quota(self):
        token = TokenInfo(token="token-heavy", quota=23)
        mgr = _build_manager(heavy_tokens=[token])

        with patch(
            "app.services.token.manager.log_call_success",
            new=AsyncMock(return_value=None),
        ):
            consumed = await mgr.consume("token-heavy", EffortType.HIGH)

        self.assertTrue(consumed)
        self.assertEqual(token.quota, 23)
        self.assertEqual(token.use_count, 1)
        self.assertIsNotNone(token.last_used_at)

    async def test_heavy_sync_usage_keeps_local_quota_unchanged(self):
        token = TokenInfo(token="token-heavy", quota=31)
        mgr = _build_manager(heavy_tokens=[token])

        with patch(
            "app.services.grok.batch_services.usage.UsageService.get",
            new=AsyncMock(return_value={"remainingTokens": 5}),
        ):
            synced = await mgr.sync_usage("token-heavy", consume_on_fail=False)

        self.assertTrue(synced)
        self.assertEqual(token.quota, 31)
        self.assertEqual(token.use_count, 1)
        self.assertIsNotNone(token.last_sync_at)


class HeavyQuotaLoadCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_heavy_load_normalizes_legacy_global_cooling(self):
        class _DummyStorage:
            async def load_tokens(self):
                return {
                    HEAVY_POOL_NAME: [
                        {
                            "token": "token-heavy",
                            "status": "cooling",
                            "quota": 0,
                            "cooling_until": 4102444800000,
                            "suspected_rate_limited_until": 4102444800000,
                        }
                    ]
                }

            async def save_tokens(self, payload):
                return payload

        mgr = TokenManager()

        with patch(
            "app.services.token.manager.get_storage",
            return_value=_DummyStorage(),
        ):
            await mgr._load()

        token = mgr.pools[HEAVY_POOL_NAME].get("token-heavy")
        self.assertIsNotNone(token)
        self.assertEqual(token.status, TokenStatus.ACTIVE)
        self.assertIsNone(token.cooling_until)
        self.assertFalse(token.is_soft_rate_limited())


if __name__ == "__main__":
    unittest.main()
