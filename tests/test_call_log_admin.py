import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin.call_log import router as call_log_router
from app.core.call_log_legacy import load_legacy_call_logs
from app.core.call_log_store import CallLogStoreFactory, get_call_log_store


def _record(index: int, *, status: str = "success", token: str = "", pool: str = "") -> dict:
    return {
        "id": f"log-{index}",
        "created_at": 1_710_000_000_000 + index,
        "status": status,
        "api_type": "chat.completions",
        "model": "grok-3",
        "email": f"user-{index % 30}@example.com",
        "token": token or f"token-{index % 30}",
        "pool": pool or f"pool-{index % 3}",
        "duration_ms": index * 10,
        "trace_id": f"trace-{index}",
        "error_code": "" if status == "success" else "internal_error",
        "error_message": "" if status == "success" else "failed",
    }


class CallLogAdminApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "SERVER_STORAGE_TYPE": "local",
                "CALL_LOG_DB_URL": str(Path(self.temp_dir.name) / "call_logs.db"),
            },
            clear=False,
        )
        self.env.start()
        asyncio.run(CallLogStoreFactory.close())

        self.app = FastAPI()
        self.app.include_router(call_log_router, prefix="/v1/admin")
        self.client = TestClient(self.app)
        self.auth_headers = {"Authorization": "Bearer grok2api"}

        asyncio.run(self._seed_records())

    def tearDown(self):
        self.client.close()
        asyncio.run(CallLogStoreFactory.close())
        self.env.stop()
        self.temp_dir.cleanup()

    async def _seed_records(self):
        store = get_call_log_store()
        for index in range(125):
            status = "success" if index % 2 == 0 else "fail"
            await store.append_call_log(_record(index, status=status))

    def test_requires_admin_auth(self):
        response = self.client.get("/v1/admin/call-logs")
        self.assertEqual(response.status_code, 401)

    def test_query_defaults_to_latest_hundred_and_keeps_full_summary(self):
        response = self.client.get("/v1/admin/call-logs", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(len(payload["items"]), 100)
        self.assertEqual(payload["pagination"]["page_size"], 100)
        self.assertEqual(payload["summary"]["total_calls"], 125)
        self.assertEqual(payload["pagination"]["total_pages"], 2)
        self.assertEqual(payload["items"][0]["id"], "log-124")
        self.assertEqual(payload["items"][-1]["id"], "log-25")
        self.assertEqual(payload["summary"]["unique_accounts"], 30)
        self.assertEqual(len(payload["accounts"]), 20)

        second_page = self.client.get(
            "/v1/admin/call-logs?page=2", headers=self.auth_headers
        )
        self.assertEqual(second_page.status_code, 200)
        second_payload = second_page.json()
        self.assertEqual(len(second_payload["items"]), 25)
        self.assertEqual(second_payload["items"][0]["id"], "log-24")
        self.assertEqual(second_payload["items"][-1]["id"], "log-0")

    def test_export_returns_utf8_bom_csv(self):
        response = self.client.get("/v1/admin/call-logs/export", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers.get("content-type", ""))
        content = response.content.decode("utf-8-sig")
        lines = [line for line in content.splitlines() if line]
        self.assertEqual(
            lines[0],
            "created_at,status,api_type,model,email,token,pool,duration_ms,trace_id,error_code,error_message",
        )
        self.assertEqual(len(lines), 126)
        self.assertIn("log", response.headers.get("content-disposition", ""))

    def test_clear_removes_current_records(self):
        response = self.client.delete("/v1/admin/call-logs", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["deleted"], 125)

        after_clear = self.client.get("/v1/admin/call-logs", headers=self.auth_headers)
        self.assertEqual(after_clear.status_code, 200)
        self.assertEqual(after_clear.json()["summary"]["total_calls"], 0)


class CallLogLegacyLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_local_jsonl_style_records(self):
        class LocalStorage:
            async def _read_call_logs(self):
                return [_record(1)]

        with patch("app.core.call_log_legacy.get_storage", return_value=LocalStorage()):
            source, records = await load_legacy_call_logs()

        self.assertEqual(source, "local")
        self.assertEqual(records[0]["id"], "log-1")

    async def test_loads_redis_style_records(self):
        class RedisStorage:
            async def _load_call_logs(self):
                return [_record(2, status="fail")]

        with patch("app.core.call_log_legacy.get_storage", return_value=RedisStorage()):
            source, records = await load_legacy_call_logs()

        self.assertEqual(source, "redis")
        self.assertEqual(records[0]["status"], "fail")

    async def test_loads_sql_style_records(self):
        class SQLStorage:
            async def _load_call_logs(self):
                return [_record(3)]

        with patch("app.core.call_log_legacy.get_storage", return_value=SQLStorage()):
            source, records = await load_legacy_call_logs()

        self.assertEqual(source, "sql")
        self.assertEqual(records[0]["trace_id"], "trace-3")


class CallLogMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "SERVER_STORAGE_TYPE": "local",
                "CALL_LOG_DB_URL": str(Path(self.temp_dir.name) / "call_logs.db"),
            },
            clear=False,
        )
        self.env.start()
        await CallLogStoreFactory.close()

    async def asyncTearDown(self):
        await CallLogStoreFactory.close()
        self.env.stop()
        self.temp_dir.cleanup()

    async def test_prepare_imports_legacy_records_and_marks_completed(self):
        with patch(
            "app.core.call_log_store.load_legacy_call_logs",
            new=AsyncMock(return_value=("local", [_record(10), _record(11)])),
        ):
            store = get_call_log_store()
            await store.prepare()
            status = await store.get_migration_status()
            result = await store.query_call_logs()

        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["migrated_count"], 2)
        self.assertEqual(result["summary"]["total_calls"], 2)

    async def test_prepare_marks_failed_but_keeps_new_writes_available(self):
        with patch(
            "app.core.call_log_store.load_legacy_call_logs",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            store = get_call_log_store()
            await store.prepare()
            status = await store.get_migration_status()
            await store.append_call_log(_record(12))
            result = await store.query_call_logs()

        self.assertEqual(status["state"], "failed")
        self.assertEqual(result["summary"]["total_calls"], 1)

