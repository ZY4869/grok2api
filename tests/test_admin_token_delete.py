import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin.token import router as token_router


class _FakeStorage:
    def __init__(self, payload):
        self.payload = deepcopy(payload)

    @asynccontextmanager
    async def acquire_lock(self, name: str, timeout: int = 10):
        yield

    async def load_tokens(self):
        return deepcopy(self.payload)

    async def save_tokens_delta(self, updated, deleted=None):
        deleted_set = set(deleted or [])
        for pool_name, items in list(self.payload.items()):
            if not isinstance(items, list):
                continue
            filtered = []
            for item in items:
                if isinstance(item, str):
                    token = item
                elif isinstance(item, dict):
                    token = item.get("token")
                else:
                    token = None
                if token and token in deleted_set:
                    continue
                filtered.append(item)
            self.payload[pool_name] = filtered


class TokenDeleteAdminTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(token_router, prefix="/v1/admin")
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer grok2api"}

    def tearDown(self):
        self.client.close()

    def test_delete_tokens_removes_unique_values_across_pools(self):
        storage = _FakeStorage(
            {
                "ssoBasic": [
                    {"token": "token-a", "email": "a@example.com"},
                    {"token": "token-b"},
                ],
                "ssoSuper": [
                    {"token": "token-a"},
                    {"token": "token-c"},
                ],
            }
        )
        manager = SimpleNamespace(reload=AsyncMock())

        with patch("app.api.v1.admin.token.get_storage", return_value=storage), patch(
            "app.api.v1.admin.token.get_token_manager",
            new=AsyncMock(return_value=manager),
        ):
            response = self.client.post(
                "/v1/admin/tokens/delete",
                headers=self.headers,
                json={"tokens": ["token-a", "token-a", "token-c"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "success", "requested": 2, "deleted": 2},
        )
        self.assertEqual(storage.payload["ssoBasic"], [{"token": "token-b"}])
        self.assertEqual(storage.payload["ssoSuper"], [])
        manager.reload.assert_awaited_once()

    def test_delete_tokens_requires_non_empty_request(self):
        response = self.client.post(
            "/v1/admin/tokens/delete",
            headers=self.headers,
            json={"tokens": []},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "No tokens provided")


if __name__ == "__main__":
    unittest.main()
