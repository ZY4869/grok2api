import unittest
from unittest.mock import AsyncMock, patch

import orjson

from app.services.grok.services.chat import ChatService
from app.services.grok.services.responses import ResponsesService
from app.services.reverse.app_chat import AppChatRequestMetadata, AppChatRequestResult
from app.services.token.quota import TokenSelectionResult


PREVIEW_URL = "https://assets.grok.com/users/u/generated/task-part-0/image.jpg"
FINAL_URL = "https://assets.grok.com/users/u/generated/task/image.jpg"
CONVERSATION_ID = "123e4567-e89b-12d3-a456-426614174000"


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


def _chat_config(key, default=None):
    values = {
        "app.thinking": False,
        "app.stream": False,
        "app.filter_tags": [],
        "retry.max_retry": 3,
        "chat.timeout": 1,
        "chat.stream_timeout": 1,
        "image.final_timeout": 1,
        "asset.download_timeout": 1,
        "proxy.browser": "chrome124",
        "proxy.user_agent": "Mozilla/5.0",
    }
    return values.get(key, default)


async def _raw_stream(*payloads):
    for payload in payloads:
        yield f"data: {orjson.dumps(payload).decode()}"


def _request_result(
    *payloads,
    conversation_id: str = CONVERSATION_ID,
    response_id: str = "resp-1",
):
    return AppChatRequestResult(
        stream=_raw_stream(*payloads),
        metadata=AppChatRequestMetadata(
            conversation_id=conversation_id,
            response_id=response_id,
        ),
    )


def _preview_payload():
    return {
        "result": {
            "response": {
                "streamingImageGenerationResponse": {
                    "imageIndex": 0,
                    "progress": 42,
                    "preview": {"assetUrl": PREVIEW_URL},
                }
            }
        }
    }


def _text_payload(text: str = "Working on it"):
    return {
        "result": {
            "response": {
                "modelResponse": {
                    "responseId": "resp-1",
                    "message": text,
                }
            }
        }
    }


def _token_payload(text: str = "Working on it"):
    return {"result": {"response": {"token": text}}}


def _final_payload(text: str = "Working on it"):
    return {
        "result": {
            "response": {
                "streamingImageGenerationResponse": {
                    "final": {"imageUrl": FINAL_URL}
                },
                "modelResponse": {
                    "responseId": "resp-1",
                    "message": text,
                },
            }
        }
    }


async def _render_image(url: str, token: str, image_id: str = "image") -> str:
    return f"![{image_id}]({url})"


class QuickImageWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_stream_state_wait_recovers_image_after_short_text_without_prompt(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_text_payload("Okay"))

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(return_value="token-a"),
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(return_value=[FINAL_URL]),
        ) as poll_mock, patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "hello there"}],
                stream=False,
            )

        content = result["choices"][0]["message"]["content"]
        self.assertIn("Okay", content)
        self.assertIn(FINAL_URL, content)
        poll_mock.assert_awaited_once()

    async def test_non_stream_state_wait_recovers_image_after_empty_text(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_text_payload(""))

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(return_value="token-a"),
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(return_value=[FINAL_URL]),
        ) as poll_mock, patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "hello there"}],
                stream=False,
            )

        content = result["choices"][0]["message"]["content"]
        self.assertIn(FINAL_URL, content)
        poll_mock.assert_awaited_once()

    async def test_non_stream_waits_for_asset_fallback_on_all_quick_models(self):
        for model in ("grok-auto", "grok-3-fast", "grok-4-expert"):
            token_mgr = DummyChatTokenManager()
            request_result = _request_result(_preview_payload(), _text_payload())

            with self.subTest(model=model), patch(
                "app.services.grok.services.chat.get_token_manager",
                new=AsyncMock(return_value=token_mgr),
            ), patch(
                "app.services.grok.services.chat.get_config",
                side_effect=_chat_config,
            ), patch(
                "app.services.grok.services.chat.select_token_for_requirement",
                new=AsyncMock(
                    return_value=TokenSelectionResult(token="token-a", total_candidates=1)
                ),
            ), patch(
                "app.services.grok.services.chat.GrokChatService.chat_openai",
                new=AsyncMock(return_value=(request_result, False, model)),
            ), patch(
                "app.services.grok.services.chat.AppChatConversationReverse.request",
                new=AsyncMock(return_value={}),
            ) as conversation_mock, patch(
                "app.services.grok.services.chat.AppAssetReverse.probe",
                new=AsyncMock(return_value=True),
            ) as asset_mock, patch(
                "app.services.grok.utils.download.DownloadService.render_image",
                new=AsyncMock(side_effect=_render_image),
            ):
                result = await ChatService.completions(
                    model=model,
                    messages=[{"role": "user", "content": "generate an image of a cat"}],
                    stream=False,
                )

            content = result["choices"][0]["message"]["content"]
            self.assertIn("Working on it", content)
            self.assertIn(f"![task]({FINAL_URL})", content)
            self.assertNotIn(PREVIEW_URL, content)
            conversation_mock.assert_awaited_once()
            asset_mock.assert_awaited_once()

    async def test_non_stream_uses_asset_fallback_without_conversation_id(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(
            _preview_payload(),
            _text_payload("Still working"),
            conversation_id="",
        )

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(return_value="token-a"),
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat.AppChatConversationReverse.request",
            new=AsyncMock(),
        ) as conversation_mock, patch(
            "app.services.grok.services.chat.AppAssetReverse.probe",
            new=AsyncMock(return_value=True),
        ) as asset_mock, patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "generate an image of a cat"}],
                stream=False,
            )

        content = result["choices"][0]["message"]["content"]
        self.assertIn("Still working", content)
        self.assertIn(FINAL_URL, content)
        conversation_mock.assert_not_awaited()
        asset_mock.assert_awaited_once()

    async def test_non_stream_skips_asset_probe_when_conversations_v2_has_final_image(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_preview_payload(), _text_payload())

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(return_value="token-a"),
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat.AppChatConversationReverse.request",
            new=AsyncMock(
                return_value={"taskResult": {"images": [{"imageUrl": FINAL_URL}]}}
            ),
        ) as conversation_mock, patch(
            "app.services.grok.services.chat.AppAssetReverse.probe",
            new=AsyncMock(return_value=False),
        ) as asset_mock, patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "draw an image of a cat"}],
                stream=False,
            )

        content = result["choices"][0]["message"]["content"]
        self.assertIn(FINAL_URL, content)
        conversation_mock.assert_awaited_once()
        asset_mock.assert_not_awaited()

    async def test_non_stream_skips_wait_when_primary_stream_has_final_image(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_final_payload())

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(return_value="token-a"),
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(),
        ) as poll_mock, patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "draw an image of a cat"}],
                stream=False,
            )

        content = result["choices"][0]["message"]["content"]
        self.assertIn(FINAL_URL, content)
        poll_mock.assert_not_awaited()

    async def test_non_stream_degrades_to_text_without_conversation_id_and_without_preview(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_text_payload("Still working"), conversation_id="")

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(return_value=[FINAL_URL]),
        ) as poll_mock, patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "generate an image of a cat"}],
                stream=False,
            )

        content = result["choices"][0]["message"]["content"]
        self.assertEqual(content, "Still working")
        self.assertNotIn(FINAL_URL, content)
        poll_mock.assert_not_awaited()

    async def test_non_stream_degrades_to_text_when_asset_probe_times_out(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_preview_payload(), _text_payload("Still working"))

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat.AppChatConversationReverse.request",
            new=AsyncMock(return_value={}),
        ) as conversation_mock, patch(
            "app.services.grok.services.chat.AppAssetReverse.probe",
            new=AsyncMock(return_value=False),
        ) as asset_mock, patch(
            "app.services.grok.services.chat._quick_image_now",
            side_effect=[0.0, 0.1, 1.2],
        ), patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "generate an image of a cat"}],
                stream=False,
            )

        content = result["choices"][0]["message"]["content"]
        self.assertEqual(content, "Still working")
        self.assertNotIn(FINAL_URL, content)
        conversation_mock.assert_awaited_once()
        asset_mock.assert_awaited_once()

    async def test_non_stream_state_wait_skips_long_text_without_prompt(self):
        token_mgr = DummyChatTokenManager()
        long_text = "This is a normal quick-mode answer with enough characters to skip recovery."
        request_result = _request_result(_text_payload(long_text))

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(return_value="token-a"),
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(return_value=[FINAL_URL]),
        ) as poll_mock, patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "hello there"}],
                stream=False,
            )

        content = result["choices"][0]["message"]["content"]
        self.assertEqual(content, long_text)
        self.assertNotIn(FINAL_URL, content)
        poll_mock.assert_not_awaited()

    async def test_stream_waits_until_asset_fallback_returns_final_image(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_token_payload(), _preview_payload())

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, True, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(return_value=[FINAL_URL]),
        ), patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            stream = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "draw an image of a cat"}],
                stream=True,
            )
            chunks = [chunk async for chunk in stream]

        joined = "".join(chunks)
        self.assertIn("Working on it", joined)
        self.assertIn(FINAL_URL, joined)
        self.assertIn("[DONE]", joined)
        self.assertNotIn(PREVIEW_URL, joined)

    async def test_stream_state_wait_delays_done_until_recovered_image(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_token_payload("Hi"))

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.pick_token",
            new=AsyncMock(return_value="token-a"),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, True, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(return_value=[FINAL_URL]),
        ) as poll_mock, patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            stream = await ChatService.completions(
                model="grok-auto",
                messages=[{"role": "user", "content": "hello there"}],
                stream=True,
            )
            chunks = [chunk async for chunk in stream]

        joined = "".join(chunks)
        self.assertIn('"content":"Hi"', joined)
        self.assertIn(FINAL_URL, joined)
        self.assertLess(joined.index(FINAL_URL), joined.index("[DONE]"))
        poll_mock.assert_awaited_once()

    async def test_responses_service_inherits_quick_image_wait_non_stream(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_preview_payload(), _text_payload())

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, False, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(return_value=[FINAL_URL]),
        ), patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await ResponsesService.create(
                model="grok-auto",
                input_value="generate an image of a cat",
                stream=False,
            )

        text = result["output"][0]["content"][0]["text"]
        self.assertIn("Working on it", text)
        self.assertIn(FINAL_URL, text)

    async def test_responses_service_inherits_quick_image_wait_stream(self):
        token_mgr = DummyChatTokenManager()
        request_result = _request_result(_token_payload(), _preview_payload())

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=token_mgr),
        ), patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.services.chat.select_token_for_requirement",
            new=AsyncMock(
                return_value=TokenSelectionResult(token="token-a", total_candidates=1)
            ),
        ), patch(
            "app.services.grok.services.chat.GrokChatService.chat_openai",
            new=AsyncMock(return_value=(request_result, True, "grok-auto")),
        ), patch(
            "app.services.grok.services.chat._poll_quick_image_final_urls",
            new=AsyncMock(return_value=[FINAL_URL]),
        ), patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            stream = await ResponsesService.create(
                model="grok-auto",
                input_value="generate an image of a cat",
                stream=True,
            )
            chunks = [chunk async for chunk in stream]

        joined = "".join(chunks)
        self.assertIn("response.output_text.delta", joined)
        self.assertIn(FINAL_URL, joined)
        self.assertIn("response.completed", joined)


if __name__ == "__main__":
    unittest.main()
