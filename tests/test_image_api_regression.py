import io
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.chat import router as chat_router
from app.api.v1.image import router as image_router
from app.services.grok.services.image import ImageGenerationResult
from app.services.grok.services.image_edit import ImageEditResult


class DummyTokenManager:
    async def reload_if_stale(self):
        return None

    def get_token(self, pool_name: str):
        return "token"


def _zero_usage() -> dict:
    return {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
    }


class ImageApiRegressionTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(image_router, prefix="/v1")
        self.app.include_router(chat_router, prefix="/v1")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_images_generations_keeps_public_response_shape(self):
        with patch(
            "app.api.v1.image.get_token_manager",
            new=AsyncMock(return_value=DummyTokenManager()),
        ), patch(
            "app.api.v1.image.ImageGenerationService.generate",
            new=AsyncMock(
                return_value=ImageGenerationResult(
                    stream=False,
                    data=["https://cdn.example.com/generated.png"],
                    usage_override=_zero_usage(),
                )
            ),
        ):
            response = self.client.post(
                "/v1/images/generations",
                json={
                    "prompt": "draw a cat",
                    "model": "grok-imagine-1.0",
                    "n": 1,
                    "size": "1024x1024",
                    "response_format": "url",
                    "stream": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"], [{"url": "https://cdn.example.com/generated.png"}])
        self.assertIn("usage", payload)

    def test_images_edits_keeps_public_response_shape(self):
        with patch(
            "app.api.v1.image.get_token_manager",
            new=AsyncMock(return_value=DummyTokenManager()),
        ), patch(
            "app.api.v1.image.ImageEditService.edit",
            new=AsyncMock(
                return_value=ImageEditResult(
                    stream=False,
                    data=["https://cdn.example.com/edited.png"],
                )
            ),
        ):
            response = self.client.post(
                "/v1/images/edits",
                data={
                    "prompt": "edit this",
                    "model": "grok-imagine-1.0-edit",
                    "n": 1,
                    "size": "1024x1024",
                    "response_format": "url",
                    "stream": "false",
                },
                files={"image": ("input.png", io.BytesIO(b"fake-image"), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"], [{"url": "https://cdn.example.com/edited.png"}])
        self.assertIn("usage", payload)

    def test_chat_completions_image_branch_keeps_public_response_shape(self):
        with patch(
            "app.api.v1.chat.get_token_manager",
            new=AsyncMock(return_value=DummyTokenManager()),
        ), patch(
            "app.api.v1.chat.ImageGenerationService.generate",
            new=AsyncMock(
                return_value=ImageGenerationResult(
                    stream=False,
                    data=["![image](https://cdn.example.com/chat-image.png)"],
                    usage_override=_zero_usage(),
                )
            ),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "grok-imagine-1.0",
                    "messages": [{"role": "user", "content": "draw a cat"}],
                    "stream": False,
                    "image_config": {
                        "n": 1,
                        "size": "1024x1024",
                        "response_format": "url",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(
            payload["choices"][0]["message"]["content"],
            "![image](https://cdn.example.com/chat-image.png)",
        )
        self.assertIn("usage", payload)


if __name__ == "__main__":
    unittest.main()
