import unittest
from unittest.mock import AsyncMock, patch

from app.services.grok.services.chat import GrokChatService
from app.services.grok.services.image_edit import ImageEditService
from app.services.grok.services.model import ModelService
from app.services.grok.utils.process import _collect_image_references
from app.services.reverse.app_chat import (
    APP_CHAT_REQUEST_LEGACY_MODEL,
    APP_CHAT_REQUEST_MODEL_ID_AUTO,
    AppChatReverse,
)


class AppChatProtocolPayloadTests(unittest.TestCase):
    def test_build_payload_supports_model_id_auto_strategy(self):
        payload = AppChatReverse.build_payload(
            message="draw a cat",
            model="grok-3",
            mode="MODEL_MODE_FAST",
            request_strategy=APP_CHAT_REQUEST_MODEL_ID_AUTO,
            model_config_override={"temperature": 0.2},
        )

        self.assertNotIn("modelName", payload)
        self.assertNotIn("modelMode", payload)
        self.assertEqual(
            payload["responseMetadata"]["requestModelDetails"]["modelId"], "auto"
        )
        self.assertEqual(
            payload["responseMetadata"]["modelConfigOverride"],
            {"temperature": 0.2},
        )


class GrokChatProtocolForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_forwards_request_strategy(self):
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
                request_strategy=APP_CHAT_REQUEST_MODEL_ID_AUTO,
            )
            _ = [chunk async for chunk in stream]

        self.assertEqual(
            request_mock.await_args.kwargs["request_strategy"],
            APP_CHAT_REQUEST_MODEL_ID_AUTO,
        )


class ImageReferenceParsingTests(unittest.TestCase):
    def test_collects_legacy_generated_urls(self):
        refs = _collect_image_references(
            {"modelResponse": {"generatedImageUrls": ["https://cdn.example.com/a.png"]}}
        )

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].url, "https://cdn.example.com/a.png")
        self.assertEqual(refs[0].source_shape, "legacy_image_urls")

    def test_collects_render_generated_and_render_edited_image_chunks(self):
        refs = _collect_image_references(
            {
                "cardAttachment": {
                    "jsonData": (
                        '{"type":"render_generated_image","image_chunk":{"url":"https://cdn.example.com/generated.png"}}'
                    )
                },
                "modelResponse": {
                    "cardAttachmentsJson": [
                        '{"type":"render_edited_image","image_chunk":{"variants":[{"assetUrl":"/generated/edited-asset"}]}}'
                    ]
                },
            }
        )

        self.assertEqual(
            {ref.url for ref in refs},
            {
                "https://cdn.example.com/generated.png",
                "/generated/edited-asset",
            },
        )
        self.assertEqual(
            {ref.source_shape for ref in refs},
            {"card_image_chunk"},
        )

    def test_ignores_unknown_card_type_and_empty_json(self):
        refs = _collect_image_references(
            {
                "cardAttachment": {"jsonData": ""},
                "modelResponse": {
                    "cardAttachmentsJson": [
                        '{"type":"unknown","image_chunk":{"url":"https://cdn.example.com/ignore.png"}}'
                    ]
                },
            }
        )

        self.assertEqual(refs, [])


class ImageEditFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_retries_legacy_strategy_after_auto_returns_empty(self):
        service = ImageEditService()
        model_info = ModelService.get("grok-imagine-1.0-edit")

        with patch(
            "app.services.grok.services.image_edit.GrokChatService.chat",
            new=AsyncMock(side_effect=[object(), object()]),
        ) as chat_mock, patch(
            "app.services.grok.services.image_edit.ImageCollectProcessor.process",
            new=AsyncMock(
                side_effect=[[], ["https://cdn.example.com/edited.png"]]
            ),
        ):
            result = await service._collect_images(
                token="token",
                prompt="edit this",
                model_info=model_info,
                n=1,
                response_format="url",
                tool_overrides={"imageGen": True},
                model_config_override={"modelMap": {}},
            )

        self.assertEqual(result, ["https://cdn.example.com/edited.png"])
        strategies = [
            call.kwargs["request_strategy"] for call in chat_mock.await_args_list
        ]
        self.assertEqual(
            strategies,
            [APP_CHAT_REQUEST_MODEL_ID_AUTO, APP_CHAT_REQUEST_LEGACY_MODEL],
        )


if __name__ == "__main__":
    unittest.main()
