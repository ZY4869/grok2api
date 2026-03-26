import io
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.chat import router as chat_router
from app.api.v1.image import router as image_router
from app.api.v1.response import router as responses_router
from app.services.grok.services.image import ImageGenerationResult
from app.services.grok.services.image_edit import ImageEditResult
from app.services.token.models import TokenInfo


class DummyPool:
    def __init__(self, token: str):
        self._token_info = TokenInfo(token=token)

    def list(self):
        return [self._token_info]

    def get(self, token: str):
        if token == self._token_info.token:
            return self._token_info
        return None


class DummyTokenManager:
    def __init__(self):
        self._token = "token"
        self.pools = {
            "ssoBasic": DummyPool(self._token),
            "ssoSuper": DummyPool(self._token),
            "ssoHeavy": DummyPool(self._token),
        }

    async def reload_if_stale(self):
        return None

    def get_token(self, pool_name: str):
        return self._token

    def get_token_info(self, pool_name: str, exclude=None, prefer_tags=None, model_id=None):
        if exclude and self._token in exclude:
            return None
        return self.pools[pool_name].get(self._token)

    def bind_token_context(self, token: str):
        return bool(token)

    def get_rate_limit_cache_entry(self, token: str, slot: str):
        return {
            "remainingQueries": 1,
            "waitTimeSeconds": 0,
            "checkedAt": 4102444800000,
            "sourceModelName": slot,
        }

    def update_rate_limit_cache_entry(self, token: str, slot: str, payload: dict, *, checked_at=None):
        return True


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
        self.app.include_router(responses_router, prefix="/v1")
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

    def test_responses_keeps_mixed_text_and_image_content(self):
        response_object = {
            "id": "resp_123",
            "object": "response",
            "created_at": 0,
            "model": "grok-auto",
            "status": "completed",
            "output": [
                {
                    "id": "msg_123",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "好的\n![image](https://cdn.example.com/mixed-image.png)",
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
        }

        with patch(
            "app.api.v1.response.ResponsesService.create",
            new=AsyncMock(return_value=response_object),
        ):
            response = self.client.post(
                "/v1/responses",
                json={
                    "model": "grok-auto",
                    "input": "draw a cat",
                    "stream": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "response")
        self.assertEqual(
            payload["output"][0]["content"][0]["text"],
            "好的\n![image](https://cdn.example.com/mixed-image.png)",
        )


if __name__ == "__main__":
    unittest.main()
