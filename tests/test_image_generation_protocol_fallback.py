import unittest
from unittest.mock import AsyncMock, patch

import orjson

from app.core.exceptions import UpstreamException
from app.services.grok.services.image import ImageGenerationResult, ImageGenerationService
from app.services.grok.services.model import ModelService
from app.services.reverse.app_chat import (
    APP_CHAT_REQUEST_LEGACY_MODEL,
    APP_CHAT_REQUEST_MODEL_ID_AUTO,
)


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


class ImageGenerationProtocolFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_retries_legacy_strategy_before_ws(self):
        service = ImageGenerationService()
        model_info = ModelService.get("grok-imagine-1.0")
        auto_result = ImageGenerationResult(
            stream=True,
            data=_async_stream(
                [_image_progress_chunk()],
                UpstreamException("auto failed", details={"status": 502}),
            ),
        )
        legacy_result = ImageGenerationResult(
            stream=True,
            data=_async_stream([_image_completed_chunk("https://cdn.example.com/legacy.png")]),
        )

        with patch.object(
            service,
            "_stream_app_chat",
            new=AsyncMock(side_effect=[auto_result, legacy_result]),
        ) as stream_mock, patch.object(service, "_stream_ws", new=AsyncMock()) as ws_mock:
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
                _image_completed_chunk("https://cdn.example.com/legacy.png"),
            ],
        )
        self.assertEqual(
            [call.kwargs["request_strategy"] for call in stream_mock.await_args_list],
            [APP_CHAT_REQUEST_MODEL_ID_AUTO, APP_CHAT_REQUEST_LEGACY_MODEL],
        )
        ws_mock.assert_not_awaited()

    async def test_collect_retries_legacy_strategy_before_ws(self):
        service = ImageGenerationService()
        model_info = ModelService.get("grok-imagine-1.0")

        with patch.object(
            service,
            "_collect_app_chat",
            new=AsyncMock(
                side_effect=[
                    [],
                    ["https://cdn.example.com/legacy.png"],
                ]
            ),
        ) as collect_mock, patch.object(service, "_collect_ws", new=AsyncMock()) as ws_mock:
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

        self.assertEqual(result.data, ["https://cdn.example.com/legacy.png"])
        self.assertEqual(
            [call.kwargs["request_strategy"] for call in collect_mock.await_args_list],
            [APP_CHAT_REQUEST_MODEL_ID_AUTO, APP_CHAT_REQUEST_LEGACY_MODEL],
        )
        ws_mock.assert_not_awaited()

    async def test_collect_uses_ws_only_after_both_app_chat_strategies_fail(self):
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
            new=AsyncMock(
                side_effect=[
                    UpstreamException("auto failed", details={"status": 502}),
                    UpstreamException("legacy failed", details={"status": 502}),
                ]
            ),
        ) as collect_mock, patch.object(
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
        self.assertEqual(
            [call.kwargs["request_strategy"] for call in collect_mock.await_args_list],
            [APP_CHAT_REQUEST_MODEL_ID_AUTO, APP_CHAT_REQUEST_LEGACY_MODEL],
        )
        ws_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
