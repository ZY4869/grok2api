import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.function.imagine import router as imagine_router
from app.services.grok.services.chat import AutoImageStreamProbe, ChatService
from app.services.token.quota import TokenSelectionResult, image_limit_exception


async def _stream_chunks(*chunks):
    for chunk in chunks:
        yield chunk


def _chat_config(key, default=None):
    values = {
        "app.thinking": False,
        "app.stream": False,
        "retry.max_retry": 3,
    }
    return values.get(key, default)


class DummyChatTokenManager:
    def __init__(self):
        self.consume = AsyncMock(return_value=True)
        self.mark_rate_limited = AsyncMock(return_value=True)

    async def reload_if_stale(self):
        return None

    def has_entitled_token_for_model(self, model: str) -> bool:
        return True

    def model_access_denial_reason(self, model: str) -> str:
        return ""

    def has_available_token_for_model(self, model: str, exclude=None) -> bool:
        return True


class AutoImageChatFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_non_stream_retries_next_token_when_first_hits_image_limit(self):
        token_mgr = DummyChatTokenManager()

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                side_effect=[
                    TokenSelectionResult(token="token-a", total_candidates=2),
                    TokenSelectionResult(token="token-b", total_candidates=2),
                ]
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(
                side_effect=[
                    (object(), False, "grok-auto"),
                    (object(), False, "grok-auto"),
                ]
            ),
        ), patch(
            "app.services.grok.services.chat.CollectProcessor.process",
            new=AsyncMock(
                side_effect=[
                    {
                        "choices": [
                            {"message": {"content": "I can help with that."}}
                        ]
                    },
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "Sure\n![image](https://cdn.example.com/cat.png)"
                                }
                            }
                        ]
                    },
                ]
            ),
        ), patch(
            "app.services.grok.services.chat.confirm_quota_exhausted",
            new=AsyncMock(side_effect=[True]),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "generate an image of a cat"}],
                stream=False,
            )

        self.assertIn("![image](https://cdn.example.com/cat.png)", result["choices"][0]["message"]["content"])
        token_mgr.consume.assert_awaited_once()
        self.assertEqual(token_mgr.consume.await_args.args[0], "token-b")
        token_mgr.mark_rate_limited.assert_not_awaited()

    async def test_auto_stream_buffers_failed_attempt_and_only_emits_retry_result(self):
        token_mgr = DummyChatTokenManager()

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=lambda key, default=None: {
                "app.thinking": False,
                "app.stream": True,
                "retry.max_retry": 3,
            }.get(key, default),
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                side_effect=[
                    TokenSelectionResult(token="token-a", total_candidates=2),
                    TokenSelectionResult(token="token-b", total_candidates=2),
                ]
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(
                side_effect=[
                    (object(), True, "grok-auto"),
                    (object(), True, "grok-auto"),
                ]
            ),
        ), patch(
            "app.services.grok.services.chat._probe_auto_image_stream",
            new=AsyncMock(
                side_effect=[
                    AutoImageStreamProbe(
                        stream=_stream_chunks("data: bad-first-attempt\n\n"),
                        stream_completed=True,
                        found_image_refs=False,
                        has_streaming_image_event=True,
                    ),
                    AutoImageStreamProbe(
                        stream=_stream_chunks("data: good-second-attempt\n\n"),
                        stream_completed=False,
                        found_image_refs=True,
                        has_streaming_image_event=True,
                    ),
                ]
            ),
        ), patch(
            "app.services.grok.services.chat.confirm_quota_exhausted",
            new=AsyncMock(side_effect=[True]),
        ):
            stream = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "draw an image of a cat"}],
                stream=True,
            )
            chunks = [chunk async for chunk in stream]

        joined = "".join(chunks)
        self.assertNotIn("bad-first-attempt", joined)
        self.assertIn("good-second-attempt", joined)
        token_mgr.consume.assert_awaited_once()
        self.assertEqual(token_mgr.consume.await_args.args[0], "token-b")

    async def test_auto_plain_text_request_does_not_use_image_quota_selection(self):
        token_mgr = DummyChatTokenManager()

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(return_value="token-a"),
        ) as pick_mock, patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(),
        ) as select_mock, patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(object(), False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat.CollectProcessor.process",
            new=AsyncMock(
                return_value={
                    "choices": [{"message": {"content": "hello world"}}]
                }
            ),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "hello there"}],
                stream=False,
            )

        self.assertEqual(result["choices"][0]["message"]["content"], "hello world")
        pick_mock.assert_awaited_once()
        select_mock.assert_not_awaited()


class ImagineLimitEventTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(imagine_router, prefix="/v1/function")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_sse_returns_image_limit_event_when_all_accounts_are_exhausted(self):
        with patch(
            "app.api.v1.function.imagine.get_function_api_key",
            return_value=None,
        ), patch(
            "app.api.v1.function.imagine.is_function_enabled",
            return_value=True,
        ), patch(
            "app.api.v1.function.imagine._select_imagine_token",
            new=AsyncMock(side_effect=image_limit_exception(1)),
        ), patch(
            "app.api.v1.function.imagine.get_token_manager",
            new=AsyncMock(return_value=DummyChatTokenManager()),
        ):
            response = self.client.get(
                "/v1/function/imagine/sse",
                params={"prompt": "draw a cat", "aspect_ratio": "1:1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("image_generation_limit_reached", response.text)


if __name__ == "__main__":
    unittest.main()
