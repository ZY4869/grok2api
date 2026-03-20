import unittest
from unittest.mock import AsyncMock, patch

import orjson

from app.services.grok.services.chat import GrokChatService
from app.services.grok.services.image import ImageGenerationResult, ImageGenerationService
from app.services.grok.services.model import ModelService
from app.services.reverse.app_chat import AppChatReverse
from app.core.exceptions import UpstreamException


def _async_stream(items, error: Exception | None = None):
    async def _gen():
        for item in items:
            yield item
        if error is not None:
            raise error

    return _gen()


def _image_completed_chunk(url: str = "https://cdn.example.com/final.png") -> str:
    payload = {
        "type": "image_generation.completed",
        "url": url,
        "usage": {
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
        },
    }
    return f"event: image_generation.completed\ndata: {orjson.dumps(payload).decode()}\n\n"


def _image_progress_chunk() -> str:
    payload = {
        "type": "image_generation.partial_image",
        "url": "",
        "index": 0,
        "progress": 42,
    }
    return f"event: image_generation.partial_image\ndata: {orjson.dumps(payload).decode()}\n\n"


def _chat_image_chunk(content: str) -> str:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "grok-imagine-1.0",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}}],
    }
    return f"event: chat.completion.chunk\ndata: {orjson.dumps(payload).decode()}\n\n"


class AppChatReversePayloadTests(unittest.TestCase):
    def test_build_payload_applies_request_overrides_without_clobbering_tool_or_model_overrides(self):
        payload = AppChatReverse.build_payload(
            message="draw a cat",
            model="grok-3",
            mode="MODEL_MODE_FAST",
            request_overrides={
                "imageGenerationCount": 4,
                "enableNsfw": True,
                "responseMetadata": {"customFlag": True},
                "toolOverrides": {"ignored": True},
            },
            tool_overrides={"imageGen": True},
            model_config_override={"temperature": 0.2},
        )

        self.assertEqual(payload["imageGenerationCount"], 4)
        self.assertTrue(payload["enableNsfw"])
        self.assertEqual(payload["toolOverrides"], {"imageGen": True})
        self.assertTrue(payload["responseMetadata"]["customFlag"])
        self.assertEqual(
            payload["responseMetadata"]["modelConfigOverride"],
            {"temperature": 0.2},
        )


class GrokChatServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_forwards_request_overrides(self):
        async def _empty_stream():
            if False:
                yield b""

        with patch(
            "app.services.grok.services.chat.AppChatReverse.request",
            new=AsyncMock(return_value=_empty_stream()),
        ) as request_mock, patch(
            "app.services.grok.services.chat.get_config",
            side_effect=lambda key, default=None: 1 if key == "chat.concurrent" else default,
        ):
            stream = await GrokChatService().chat(
                token="token",
                message="draw a cat",
                model="grok-3",
                mode="MODEL_MODE_FAST",
                stream=True,
                request_overrides={"enableNsfw": True},
            )
            chunks = [chunk async for chunk in stream]

        self.assertEqual(chunks, [])
        self.assertEqual(
            request_mock.await_args.kwargs["request_overrides"],
            {"enableNsfw": True},
        )


class ImageGenerationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_success_uses_app_chat_without_ws_fallback(self):
        service = ImageGenerationService()
        model_info = ModelService.get("grok-imagine-1.0")
        app_chat_result = ImageGenerationResult(
            stream=True,
            data=_async_stream([_image_completed_chunk()]),
        )

        with patch.object(
            service,
            "_stream_app_chat",
            new=AsyncMock(return_value=app_chat_result),
        ), patch.object(service, "_stream_ws", new=AsyncMock()) as ws_mock:
            result = await service._stream_with_fallback(
                token="token",
                model_info=model_info,
                prompt="draw a cat",
                n=1,
                response_format="url",
                size="1024x1024",
                aspect_ratio="1:1",
                enable_nsfw=False,
                chat_format=False,
            )
            chunks = [chunk async for chunk in result.data]

        self.assertEqual(chunks, [_image_completed_chunk()])
        ws_mock.assert_not_awaited()

    async def test_stream_falls_back_when_app_chat_fails_before_first_valid_chunk(self):
        service = ImageGenerationService()
        model_info = ModelService.get("grok-imagine-1.0")
        app_chat_result = ImageGenerationResult(
            stream=True,
            data=_async_stream(
                [_image_progress_chunk()],
                UpstreamException("app-chat failed", details={"status": 502}),
            ),
        )
        ws_result = ImageGenerationResult(
            stream=True,
            data=_async_stream([_image_completed_chunk("https://cdn.example.com/ws.png")]),
        )

        with patch.object(
            service,
            "_stream_app_chat",
            new=AsyncMock(return_value=app_chat_result),
        ), patch.object(
            service,
            "_stream_ws",
            new=AsyncMock(return_value=ws_result),
        ) as ws_mock:
            result = await service._stream_with_fallback(
                token="token",
                model_info=model_info,
                prompt="draw a cat",
                n=1,
                response_format="url",
                size="1024x1024",
                aspect_ratio="1:1",
                enable_nsfw=False,
                chat_format=False,
            )
            chunks = [chunk async for chunk in result.data]

        self.assertEqual(
            chunks,
            [
                _image_progress_chunk(),
                _image_completed_chunk("https://cdn.example.com/ws.png"),
            ],
        )
        ws_mock.assert_awaited_once()

    async def test_stream_does_not_fallback_after_first_valid_chunk(self):
        service = ImageGenerationService()
        model_info = ModelService.get("grok-imagine-1.0")
        app_chat_result = ImageGenerationResult(
            stream=True,
            data=_async_stream(
                [_chat_image_chunk("![image](https://cdn.example.com/app-chat.png)")],
                UpstreamException("late failure", details={"status": 502}),
            ),
        )

        with patch.object(
            service,
            "_stream_app_chat",
            new=AsyncMock(return_value=app_chat_result),
        ), patch.object(service, "_stream_ws", new=AsyncMock()) as ws_mock:
            result = await service._stream_with_fallback(
                token="token",
                model_info=model_info,
                prompt="draw a cat",
                n=1,
                response_format="url",
                size="1024x1024",
                aspect_ratio="1:1",
                enable_nsfw=False,
                chat_format=True,
            )

            chunks = []
            with self.assertRaises(UpstreamException):
                async for chunk in result.data:
                    chunks.append(chunk)

        self.assertEqual(
            chunks,
            [_chat_image_chunk("![image](https://cdn.example.com/app-chat.png)")],
        )
        ws_mock.assert_not_awaited()

    async def test_collect_falls_back_when_app_chat_returns_empty(self):
        service = ImageGenerationService()
        model_info = ModelService.get("grok-imagine-1.0")
        ws_result = ImageGenerationResult(
            stream=False,
            data=["https://cdn.example.com/ws.png"],
            usage_override={"total_tokens": 0},
        )

        with patch.object(
            service,
            "_collect_app_chat",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            service,
            "_collect_ws",
            new=AsyncMock(return_value=ws_result),
        ) as ws_mock:
            result = await service._collect_with_fallback(
                token_mgr=object(),
                token="token",
                model_info=model_info,
                tried_tokens={"token"},
                prompt="draw a cat",
                n=1,
                response_format="url",
                aspect_ratio="1:1",
                enable_nsfw=False,
            )

        self.assertIs(result, ws_result)
        ws_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
