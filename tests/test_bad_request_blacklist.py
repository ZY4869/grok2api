import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.exceptions import UpstreamException
from app.services.grok.services.chat import ChatService
from app.services.grok.services.image import ImageGenerationResult, ImageGenerationService
from app.services.token.manager import TokenManager
from app.services.token.models import TokenInfo, TokenStatus
from app.services.token.pool import TokenPool
from app.services.token.quota import TokenSelectionResult, select_token_for_requirement


def _build_manager(*, basic_tokens=None) -> TokenManager:
    mgr = TokenManager()
    mgr.initialized = True
    pool = TokenPool("ssoBasic")
    for token in basic_tokens or []:
        pool.add(token)
    mgr.pools = {"ssoBasic": pool}
    return mgr


def _chat_config(key, default=None):
    values = {
        "app.thinking": False,
        "app.stream": False,
        "retry.max_retry": 3,
    }
    return values.get(key, default)


class RuntimeSelectionGovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_selection_filters_alive_false_blacklisted_and_bad_request_cooling(self):
        alive_false = TokenInfo(token="token-dead", quota=9, alive=False)
        blacklisted = TokenInfo(
            token="token-blacklisted",
            quota=9,
            status=TokenStatus.BLACKLISTED,
            blacklisted_at=1,
            delete_after_at=2,
        )
        quarantined = TokenInfo(
            token="token-quarantined",
            quota=9,
            bad_request_fail_count=1,
            bad_request_cooling_until=4102444800000,
        )
        healthy = TokenInfo(token="token-healthy", quota=9)
        mgr = _build_manager(
            basic_tokens=[alive_false, blacklisted, quarantined, healthy]
        )

        self.assertEqual(mgr.get_token("ssoBasic"), "token-healthy")
        token_info = mgr.get_token_info("ssoBasic")
        self.assertIsNotNone(token_info)
        self.assertEqual(token_info.token, "token-healthy")
        self.assertTrue(mgr.has_available_token_for_model("grok-3"))

        selection = await select_token_for_requirement(
            mgr,
            "grok-3",
            tried=set(),
        )
        self.assertEqual(selection.token, "token-healthy")

    async def test_bad_request_governance_quarantines_then_blacklists_and_recovers(self):
        token = TokenInfo(token="token-a", quota=12)
        mgr = _build_manager(basic_tokens=[token])

        first = await mgr.mark_bad_request_failed(
            "token-a",
            reason="app_chat_bad_request",
            body_summary="bad request",
        )
        self.assertTrue(first["ok"])
        self.assertEqual(first["action"], "quarantine")
        self.assertEqual(token.status, TokenStatus.ACTIVE)
        self.assertEqual(token.bad_request_fail_count, 1)
        self.assertIsNotNone(token.bad_request_cooling_until)
        self.assertIsNone(mgr.get_token("ssoBasic"))

        token.bad_request_cooling_until = mgr._now_ms() - 1
        second = await mgr.mark_bad_request_failed(
            "token-a",
            reason="app_chat_bad_request",
            body_summary="bad request again",
        )
        self.assertTrue(second["ok"])
        self.assertEqual(second["action"], "blacklist")
        self.assertEqual(token.status, TokenStatus.BLACKLISTED)
        self.assertEqual(token.bad_request_fail_count, 2)
        self.assertIsNotNone(token.blacklisted_at)
        self.assertIsNotNone(token.delete_after_at)
        self.assertIsNone(mgr.get_token("ssoBasic"))

        recovered = await mgr.recover_blacklisted("token-a")
        self.assertTrue(recovered)
        self.assertEqual(token.status, TokenStatus.ACTIVE)
        self.assertEqual(token.bad_request_fail_count, 0)
        self.assertIsNone(token.bad_request_cooling_until)
        self.assertIsNone(token.blacklisted_at)
        self.assertIsNone(token.delete_after_at)
        self.assertEqual(mgr.get_token("ssoBasic"), "token-a")

    async def test_cleanup_blacklisted_tokens_only_deletes_expired_entries(self):
        expired = TokenInfo(
            token="token-expired-blacklisted",
            quota=0,
            status=TokenStatus.BLACKLISTED,
            blacklisted_at=1,
            delete_after_at=1,
        )
        retained = TokenInfo(
            token="token-retained-blacklisted",
            quota=0,
            status=TokenStatus.BLACKLISTED,
            blacklisted_at=1,
            delete_after_at=4102444800000,
        )
        mgr = _build_manager(basic_tokens=[expired, retained])

        result = await mgr.cleanup_blacklisted_tokens()

        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(mgr.pools["ssoBasic"].get("token-expired-blacklisted"))
        self.assertIsNotNone(mgr.pools["ssoBasic"].get("token-retained-blacklisted"))


class AppChatBadRequestFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_request_retries_next_token_after_upstream_400(self):
        token_mgr = SimpleNamespace(
            reload_if_stale=AsyncMock(return_value=None),
            has_entitled_token_for_model=lambda model: True,
            model_access_denial_reason=lambda model: "",
            has_available_token_for_model=lambda model, exclude=None: True,
            bind_token_context=lambda token: True,
            mark_bad_request_failed=AsyncMock(
                return_value={"ok": True, "action": "quarantine"}
            ),
        )

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(side_effect=["token-a", "token-b"]),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(
                side_effect=[
                    UpstreamException(
                        message="AppChatReverse: Chat failed, 400",
                        details={"status": 400, "body": "bad token"},
                    ),
                    (object(), False, "grok-auto"),
                ]
            ),
        ), patch(
            "app.services.grok.services.chat.CollectProcessor.process",
            new=AsyncMock(
                return_value={"choices": [{"message": {"content": "ok"}}]}
            ),
        ), patch(
            "app.services.grok.services.chat._consume_chat_usage",
            new=AsyncMock(return_value=None),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
            )

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        token_mgr.mark_bad_request_failed.assert_awaited_once()
        self.assertEqual(
            token_mgr.mark_bad_request_failed.await_args.args[0],
            "token-a",
        )

    async def test_image_generation_retries_next_token_after_upstream_400(self):
        token_mgr = SimpleNamespace(
            mark_bad_request_failed=AsyncMock(
                return_value={"ok": True, "action": "quarantine"}
            )
        )
        service = ImageGenerationService()
        model_info = SimpleNamespace(model_id="grok-imagine-1.0")

        with patch(
            "app.services.grok.services.image.get_config",
            side_effect=lambda key, default=None: 2 if key == "retry.max_retry" else default,
        ), patch(
            "app.services.grok.services.image.select_token_for_requirement",
            new=AsyncMock(
                side_effect=[
                    TokenSelectionResult(token="token-a", total_candidates=2),
                    TokenSelectionResult(token="token-b", total_candidates=2),
                ]
            ),
        ), patch.object(
            ImageGenerationService,
            "_collect_with_fallback",
            new=AsyncMock(
                side_effect=[
                    UpstreamException(
                        message="AppChatReverse: Chat failed, 400",
                        details={"status": 400, "body": "bad image token"},
                    ),
                    ImageGenerationResult(
                        stream=False,
                        data=["https://cdn.example.com/final.png"],
                    ),
                ]
            ),
        ):
            result = await service.generate(
                token_mgr=token_mgr,
                token="token-a",
                model_info=model_info,
                prompt="draw a cat",
                n=1,
                response_format="url",
                size="1024x1024",
                aspect_ratio="1:1",
                stream=False,
                enable_nsfw=False,
            )

        self.assertEqual(result.data, ["https://cdn.example.com/final.png"])
        token_mgr.mark_bad_request_failed.assert_awaited_once()
        self.assertEqual(
            token_mgr.mark_bad_request_failed.await_args.args[0],
            "token-a",
        )


if __name__ == "__main__":
    unittest.main()
