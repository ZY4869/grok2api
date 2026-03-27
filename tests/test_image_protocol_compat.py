import unittest
from unittest.mock import AsyncMock, patch

import orjson

from app.services.grok.services.chat import CollectProcessor, GrokChatService, StreamProcessor
from app.services.grok.services.image_edit import ImageEditService
from app.services.grok.services.model import ModelService
from app.services.grok.utils.process import _collect_image_references
from app.services.reverse.app_chat import (
    APP_CHAT_REQUEST_LEGACY_MODEL,
    APP_CHAT_REQUEST_MODE_ID,
    APP_CHAT_REQUEST_MODEL_ID_AUTO,
    AppChatRequestMetadata,
    AppChatReverse,
    _update_metadata_from_line,
)


async def _response_stream(*payloads):
    for payload in payloads:
        yield f"data: {orjson.dumps(payload).decode()}\n\n".encode()


async def _render_image(*args, **kwargs):
    url = args[-3]
    image_id = args[-1] if args else kwargs.get("image_id", "image")
    return f"![{image_id}]({url})"


def _chat_config(key, default=None):
    values = {
        "app.filter_tags": [],
        "chat.stream_timeout": 1,
    }
    return values.get(key, default)


class AppChatProtocolPayloadTests(unittest.TestCase):
    def test_metadata_extractor_accepts_plain_conversation_id(self):
        metadata = AppChatRequestMetadata()
        conversation_id = "123e4567-e89b-12d3-a456-426614174000"

        _update_metadata_from_line(
            metadata,
            "data: "
            + orjson.dumps(
                {
                    "result": {
                        "response": {
                            "conversationId": conversation_id,
                            "responseId": "resp-1",
                        }
                    }
                }
            ).decode(),
        )

        self.assertEqual(metadata.conversation_id, conversation_id)
        self.assertEqual(metadata.response_id, "resp-1")

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

    def test_build_payload_mode_id_defaults_enable420_to_true(self):
        with patch(
            "app.services.reverse.app_chat.get_config",
            side_effect=lambda key, default=None: {
                "app.custom_instruction": "",
                "app.disable_memory": False,
                "app.temporary": False,
                "app.auto_enable_420": True,
            }.get(key, default),
        ):
            payload = AppChatReverse.build_payload(
                message="draw a cat",
                model="grok-3",
                mode="auto",
                use_mode_id=True,
            )

        self.assertEqual(payload["modeId"], "auto")
        self.assertTrue(payload["enable420"])

    def test_build_payload_mode_id_includes_har_defaults(self):
        with patch(
            "app.services.reverse.app_chat.get_config",
            side_effect=lambda key, default=None: {
                "app.custom_instruction": "",
                "app.disable_memory": False,
                "app.temporary": False,
                "app.auto_enable_420": True,
            }.get(key, default),
        ):
            payload = AppChatReverse.build_payload(
                message="draw a cat",
                model="grok-auto",
                mode="auto",
                request_strategy=APP_CHAT_REQUEST_MODE_ID,
            )

        self.assertEqual(payload["collectionIds"], [])
        self.assertEqual(payload["connectors"], [])
        self.assertFalse(payload["searchAllConnectors"])
        self.assertEqual(
            payload["toolOverrides"],
            {
                "gmailSearch": False,
                "googleCalendarSearch": False,
                "outlookSearch": False,
                "outlookCalendarSearch": False,
                "googleDriveSearch": False,
            },
        )

    def test_build_payload_merges_default_and_custom_tool_overrides(self):
        with patch(
            "app.services.reverse.app_chat.get_config",
            side_effect=lambda key, default=None: {
                "app.custom_instruction": "",
                "app.disable_memory": False,
                "app.temporary": False,
                "app.auto_enable_420": True,
            }.get(key, default),
        ):
            payload = AppChatReverse.build_payload(
                message="draw a cat",
                model="grok-auto",
                mode="auto",
                request_strategy=APP_CHAT_REQUEST_MODE_ID,
                request_overrides={"toolOverrides": {"gmailSearch": True}},
                tool_overrides={"googleDriveSearch": True},
            )

        self.assertTrue(payload["toolOverrides"]["gmailSearch"])
        self.assertTrue(payload["toolOverrides"]["googleDriveSearch"])
        self.assertFalse(payload["toolOverrides"]["outlookSearch"])
        self.assertFalse(payload["toolOverrides"]["outlookCalendarSearch"])


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

    def test_collects_streaming_image_generation_urls(self):
        refs = _collect_image_references(
            {
                "streamingImageGenerationResponse": {
                    "preview": {
                        "assetUrl": "https://assets.grok.com/users/u/generated/task-part-0/image.jpg"
                    },
                    "final": {
                        "imageUrl": "https://assets.grok.com/users/u/generated/task/image.jpg"
                    },
                }
            }
        )

        self.assertEqual(
            {ref.url for ref in refs},
            {
                "https://assets.grok.com/users/u/generated/task-part-0/image.jpg",
                "https://assets.grok.com/users/u/generated/task/image.jpg",
            },
        )
        self.assertEqual(
            {ref.source_shape for ref in refs},
            {"streaming_image_generation"},
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


class ChatImageProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_processor_emits_images_from_streaming_image_event(self):
        processor = StreamProcessor("grok-auto", "token", show_think=False)

        with patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            chunks = [
                chunk
                async for chunk in processor.process(
                    _response_stream(
                        {"result": {"response": {"token": "好的"}}},
                        {
                            "result": {
                                "response": {
                                    "streamingImageGenerationResponse": {
                                        "imageUrl": "https://assets.grok.com/users/u/generated/task/image.jpg"
                                    }
                                }
                            }
                        },
                    )
                )
            ]

        joined = "".join(chunks)
        self.assertIn("好的", joined)
        self.assertIn("![task](https://assets.grok.com/users/u/generated/task/image.jpg)", joined)

    async def test_collect_processor_appends_images_from_streaming_image_event(self):
        processor = CollectProcessor("grok-auto", "token")

        with patch(
            "app.services.grok.services.chat.get_config",
            side_effect=_chat_config,
        ), patch(
            "app.services.grok.utils.download.DownloadService.render_image",
            new=AsyncMock(side_effect=_render_image),
        ):
            result = await processor.process(
                _response_stream(
                    {
                        "result": {
                            "response": {
                                "streamingImageGenerationResponse": {
                                    "imageUrl": "https://assets.grok.com/users/u/generated/task/image.jpg"
                                },
                                "modelResponse": {
                                    "responseId": "resp-1",
                                    "message": "好的，我来生成。",
                                },
                            }
                        }
                    }
                )
            )

        content = result["choices"][0]["message"]["content"]
        self.assertIn("好的，我来生成。", content)
        self.assertIn(
            "![task](https://assets.grok.com/users/u/generated/task/image.jpg)",
            content,
        )


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
