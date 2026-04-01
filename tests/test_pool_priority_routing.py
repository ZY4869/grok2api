import unittest
from unittest.mock import AsyncMock, patch

from app.services.grok.services.chat import ChatService
from app.services.grok.services.model import (
    BASIC_POOL_NAME,
    DEFAULT_POOL_CANDIDATES,
    HEAVY_MODEL_ID,
    HEAVY_POOL_NAME,
    HIGH_TIER_POOL_CANDIDATES,
    ModelService,
    SUPER_POOL_NAME,
)
from app.services.grok.utils.retry import pick_token
from app.services.token.manager import TokenManager
from app.services.token.models import TokenInfo
from app.services.token.pool import TokenPool
from app.services.token.quota import QuotaProbeResult, select_token_for_requirement


def _token(token: str, *, quota: int = 10) -> TokenInfo:
    return TokenInfo(token=token, quota=quota)


def _build_manager(*, basic_tokens=None, super_tokens=None, heavy_tokens=None) -> TokenManager:
    mgr = TokenManager()
    mgr.initialized = True
    mgr.pools = {}

    basic_pool = TokenPool(BASIC_POOL_NAME)
    for info in basic_tokens or []:
        basic_pool.add(info)
    mgr.pools[BASIC_POOL_NAME] = basic_pool

    super_pool = TokenPool(SUPER_POOL_NAME)
    for info in super_tokens or []:
        super_pool.add(info)
    mgr.pools[SUPER_POOL_NAME] = super_pool

    heavy_pool = TokenPool(HEAVY_POOL_NAME)
    for info in heavy_tokens or []:
        heavy_pool.add(info)
    mgr.pools[HEAVY_POOL_NAME] = heavy_pool
    return mgr


def _chat_config(key, default=None):
    values = {
        "app.thinking": False,
        "app.stream": False,
        "retry.max_retry": 3,
    }
    return values.get(key, default)


class ModelPoolPriorityTests(unittest.TestCase):
    def test_pool_candidates_follow_high_tier_priority(self):
        self.assertEqual(
            ModelService.pool_candidates_for_model("grok-3-fast"),
            DEFAULT_POOL_CANDIDATES,
        )
        self.assertEqual(
            ModelService.pool_candidates_for_model("grok-imagine-1.0-fast"),
            HIGH_TIER_POOL_CANDIDATES,
        )
        self.assertEqual(
            ModelService.pool_candidates_for_model("grok-imagine-1.0"),
            HIGH_TIER_POOL_CANDIDATES,
        )
        self.assertEqual(
            ModelService.pool_candidates_for_model("grok-imagine-1.0-edit"),
            HIGH_TIER_POOL_CANDIDATES,
        )
        self.assertEqual(
            ModelService.pool_candidates_for_model("grok-imagine-1.0-video"),
            HIGH_TIER_POOL_CANDIDATES,
        )
        self.assertEqual(
            ModelService.pool_candidates_for_model(HEAVY_MODEL_ID),
            [HEAVY_POOL_NAME],
        )


class SelectionPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_token_prefers_heavy_pool_for_text_models(self):
        mgr = _build_manager(
            basic_tokens=[_token("token-basic")],
            heavy_tokens=[_token("token-heavy")],
        )

        token = await pick_token(mgr, "grok-3-fast", tried=set())

        self.assertEqual(token, "token-heavy")

    async def test_text_selection_falls_back_to_basic_when_high_tier_missing(self):
        mgr = _build_manager(basic_tokens=[_token("token-basic")])

        selection = await select_token_for_requirement(
            mgr,
            "grok-3-fast",
            tried=set(),
        )

        self.assertEqual(selection.token, "token-basic")

    async def test_dedicated_media_models_do_not_select_basic_pool_tokens(self):
        mgr = _build_manager(basic_tokens=[_token("token-basic")])

        for model_id in (
            "grok-imagine-1.0-fast",
            "grok-imagine-1.0",
            "grok-imagine-1.0-edit",
            "grok-imagine-1.0-video",
        ):
            with self.subTest(model_id=model_id):
                selection = await select_token_for_requirement(
                    mgr,
                    model_id,
                    tried=set(),
                )
                self.assertIsNone(selection.token)
                self.assertEqual(selection.total_candidates, 0)

    def test_video_selection_respects_explicit_pool_order(self):
        mgr = _build_manager(
            super_tokens=[_token("token-super")],
            heavy_tokens=[_token("token-heavy")],
        )

        token_info = mgr.get_token_for_video(
            resolution="720p",
            video_length=8,
            pool_candidates=[HEAVY_POOL_NAME, SUPER_POOL_NAME],
            model_id="grok-imagine-1.0-video",
        )

        self.assertIsNotNone(token_info)
        self.assertEqual(token_info.token, "token-heavy")


class ChatRoutingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_text_requests_choose_heavy_before_basic(self):
        mgr = _build_manager(
            basic_tokens=[_token("token-basic")],
            heavy_tokens=[_token("token-heavy")],
        )
        mgr.reload_if_stale = AsyncMock(return_value=None)

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(object(), False, "grok-3-fast")),
        ) as chat_openai_mock, patch(
            "app.services.grok.services.chat.CollectProcessor.process",
            new=AsyncMock(return_value={"choices": [{"message": {"content": "ok"}}]}),
        ), patch(
            "app.services.grok.services.chat._consume_chat_usage",
            new=AsyncMock(return_value=None),
        ):
            result = await ChatService.completions(
                model="grok-3-fast",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
            )

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(chat_openai_mock.await_args.args[0], "token-heavy")

    async def test_quick_image_requests_can_fall_back_to_basic_pool(self):
        mgr = _build_manager(basic_tokens=[_token("token-basic")])
        mgr.reload_if_stale = AsyncMock(return_value=None)
        probe_result = QuotaProbeResult(
            slot="grok-imagine-1.0",
            probe_model="grok-imagine-1.0",
            source_model="grok-imagine-1.0",
            remaining_queries=None,
            wait_time_seconds=None,
            checked_at=0,
            cache_hit=False,
            exhausted=False,
            known=False,
        )

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.token.quota.probe_quota",
            new=AsyncMock(return_value=probe_result),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(object(), False, "grok-auto")),
        ) as chat_openai_mock, patch(
            "app.services.grok.services.chat.CollectProcessor.process",
            new=AsyncMock(
                return_value={
                    "choices": [
                        {
                            "message": {
                                "content": "![image](https://cdn.example.com/cat.png)"
                            }
                        }
                    ]
                }
            ),
        ), patch(
            "app.services.grok.services.chat._consume_chat_usage",
            new=AsyncMock(return_value=None),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "generate an image of a cat"}],
                stream=False,
            )

        self.assertIn("![image](https://cdn.example.com/cat.png)", result["choices"][0]["message"]["content"])
        self.assertEqual(chat_openai_mock.await_args.args[0], "token-basic")


if __name__ == "__main__":
    unittest.main()
