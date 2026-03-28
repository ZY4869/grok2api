import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin.call_log import router as call_log_router
from app.core.call_log_admin import build_today_generation_window
from app.core.call_log_legacy import load_legacy_call_logs
from app.core.call_log_store import CallLogStoreFactory, get_call_log_store
from app.services.token.quota import IMAGE_LIMIT_SINGLE_MESSAGE


def _record(
    index: int,
    *,
    created_at: int | None = None,
    status: str = "success",
    token: str = "",
    pool: str | None = None,
    email: str | None = None,
    model: str = "grok-3",
    error_code: str = "",
    error_message: str = "",
) -> dict:
    return {
        "id": f"log-{index}",
        "created_at": created_at if created_at is not None else 1_710_000_000_000 + index,
        "status": status,
        "api_type": "chat.completions",
        "model": model,
        "email": f"user-{index % 30}@example.com" if email is None else email,
        "token": token or f"token-{index % 30}",
        "pool": f"pool-{index % 3}" if pool is None else pool,
        "duration_ms": index * 10,
        "trace_id": f"trace-{index}",
        "error_code": error_code if status == "fail" else "",
        "error_message": error_message if status == "fail" else "",
    }


class _DummyTokenInfo:
    def __init__(
        self,
        token: str,
        *,
        email: str = "",
        alive: bool = True,
        available: bool = True,
        status: str = "active",
    ):
        self.token = token
        self.email = email
        self.alive = alive
        self.status = status
        self._available = available

    def is_available(self, consumed_mode):
        return self._available


class _DummyPool:
    def __init__(self, infos):
        self._infos = list(infos)

    def list(self):
        return list(self._infos)


class _DummyTokenManager:
    def __init__(self, pools=None, *, reload_callback=None):
        self.pools = pools or {}
        self.reload_callback = reload_callback
        self.reload_calls = 0

    async def reload(self):
        self.reload_calls += 1
        if not callable(self.reload_callback):
            return
        result = self.reload_callback(self)
        if asyncio.iscoroutine(result):
            await result


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

        self.token_manager = _DummyTokenManager({})
        self.token_manager_patch = patch(
            "app.api.v1.admin.call_log.get_token_manager",
            new=AsyncMock(return_value=self.token_manager),
        )
        self.token_manager_patch.start()

        asyncio.run(self._seed_records())

    def tearDown(self):
        self.token_manager_patch.stop()
        self.client.close()
        asyncio.run(CallLogStoreFactory.close())
        self.env.stop()
        self.temp_dir.cleanup()

    async def _seed_records(self):
        store = get_call_log_store()
        for index in range(125):
            status = "success" if index % 2 == 0 else "fail"
            await store.append_call_log(_record(index, status=status))

    async def _reset_records(self, records: list[dict]):
        store = get_call_log_store()
        await store.clear_call_logs()
        for record in records:
            await store.append_call_log(record)

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
        self.assertEqual(payload["account_stats"]["total_accounts"], 0)
        self.assertEqual(payload["account_stats"]["available_accounts"], 0)
        self.assertEqual(payload["quick_image_limit_stats"]["total_hits"], 0)
        self.assertEqual(payload["today_generation_stats"]["timezone"], "Asia/Shanghai")
        self.assertEqual(payload["today_generation_stats"]["image_count"], 0)
        self.assertEqual(payload["today_generation_stats"]["video_count"], 0)

        second_page = self.client.get(
            "/v1/admin/call-logs?page=2", headers=self.auth_headers
        )
        self.assertEqual(second_page.status_code, 200)
        second_payload = second_page.json()
        self.assertEqual(len(second_payload["items"]), 25)
        self.assertEqual(second_payload["items"][0]["id"], "log-24")
        self.assertEqual(second_payload["items"][-1]["id"], "log-0")

    def test_query_backfills_account_fields_and_keyword_matches_current_snapshot(self):
        asyncio.run(
            self._reset_records(
                [
                    _record(
                        1001,
                        status="success",
                        token="mapped-token",
                        pool="",
                        email="",
                    )
                ]
            )
        )
        self.token_manager.pools = {
            "pool-a": _DummyPool(
                [_DummyTokenInfo("mapped-token", email="mapped@example.com", available=True)]
            )
        }

        response = self.client.get(
            "/v1/admin/call-logs?account_keyword=mapped@example.com",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["summary"]["total_calls"], 1)
        self.assertEqual(payload["items"][0]["email"], "mapped@example.com")
        self.assertEqual(payload["items"][0]["pool"], "pool-a")
        self.assertEqual(payload["items"][0]["account_display"], "mapped@example.com")
        self.assertEqual(payload["account_stats"]["total_accounts"], 1)
        self.assertEqual(payload["account_stats"]["available_accounts"], 1)
        self.assertEqual(payload["account_stats"]["called_accounts"], 1)

        export_response = self.client.get(
            "/v1/admin/call-logs/export?account_keyword=mapped@example.com",
            headers=self.auth_headers,
        )
        self.assertEqual(export_response.status_code, 200)
        content = export_response.content.decode("utf-8-sig")
        self.assertIn("mapped@example.com", content)
        self.assertIn("pool-a", content)

    def test_query_refreshes_token_snapshot_before_building_account_stats(self):
        asyncio.run(
            self._reset_records(
                [
                    _record(
                        1501,
                        status="success",
                        token="reload-token",
                        pool="",
                        email="",
                    )
                ]
            )
        )

        def _populate_pools(manager):
            manager.pools = {
                "pool-r": _DummyPool(
                    [
                        _DummyTokenInfo("reload-token", email="reload@example.com", available=True),
                        _DummyTokenInfo("spare-token", email="spare@example.com", available=False),
                    ]
                )
            }

        self.token_manager.pools = {}
        self.token_manager.reload_callback = _populate_pools

        response = self.client.get("/v1/admin/call-logs", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(self.token_manager.reload_calls, 1)
        self.assertEqual(payload["account_stats"]["total_accounts"], 2)
        self.assertEqual(payload["account_stats"]["available_accounts"], 1)
        self.assertEqual(payload["items"][0]["email"], "reload@example.com")
        self.assertEqual(payload["items"][0]["pool"], "pool-r")

    def test_quick_image_limit_stats_and_account_stats_follow_mixed_scope(self):
        asyncio.run(
            self._reset_records(
                [
                    _record(
                        2001,
                        status="fail",
                        token="quick-one",
                        pool="",
                        email="",
                        model="grok-auto",
                        error_code="image_generation_limit_reached",
                        error_message="limit",
                    ),
                    _record(
                        2002,
                        status="fail",
                        token="quick-one",
                        pool="",
                        email="",
                        model="grok-3-fast",
                        error_message="You've reached your image generation limit. Please try again later.",
                    ),
                    _record(
                        2003,
                        status="fail",
                        token="quick-two",
                        pool="",
                        email="",
                        model="grok-4-expert",
                        error_message=IMAGE_LIMIT_SINGLE_MESSAGE,
                    ),
                    _record(
                        2004,
                        status="fail",
                        token="other-token",
                        pool="",
                        email="",
                        model="grok-imagine-1.0",
                        error_message="You've reached your image generation limit. Please try again later.",
                    ),
                ]
            )
        )
        self.token_manager.pools = {
            "pool-a": _DummyPool(
                [
                    _DummyTokenInfo("quick-one", email="quick1@example.com", available=True),
                    _DummyTokenInfo("quick-two", email="quick2@example.com", available=False),
                ]
            ),
            "pool-b": _DummyPool(
                [_DummyTokenInfo("other-token", email="other@example.com", available=True)]
            ),
        }

        response = self.client.get("/v1/admin/call-logs", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        quick_stats = payload["quick_image_limit_stats"]
        self.assertEqual(quick_stats["total_hits"], 3)
        self.assertEqual(quick_stats["unique_accounts"], 2)
        self.assertEqual(quick_stats["items"][0]["email"], "quick1@example.com")
        self.assertEqual(quick_stats["items"][0]["hit_count"], 2)

        account_stats = payload["account_stats"]
        self.assertEqual(account_stats["total_accounts"], 3)
        self.assertEqual(account_stats["available_accounts"], 2)
        self.assertEqual(account_stats["limit_accounts"], 2)
        self.assertEqual(account_stats["called_accounts"], 3)

    def test_today_generation_stats_only_count_successful_dedicated_media_models(self):
        window = build_today_generation_window()
        inside_start = window["start_ms"] + 1_000
        inside_end = window["end_ms"] - 1_000
        before_today = window["start_ms"] - 1_000
        asyncio.run(
            self._reset_records(
                [
                    _record(
                        3101,
                        created_at=inside_start,
                        status="success",
                        model="grok-imagine-1.0",
                    ),
                    _record(
                        3102,
                        created_at=inside_start + 1_000,
                        status="success",
                        model="grok-imagine-1.0-edit",
                    ),
                    _record(
                        3103,
                        created_at=inside_end,
                        status="success",
                        model="grok-imagine-1.0-video",
                    ),
                    _record(
                        3104,
                        created_at=inside_start + 2_000,
                        status="success",
                        model="grok-auto",
                    ),
                    _record(
                        3105,
                        created_at=inside_start + 3_000,
                        status="fail",
                        model="grok-imagine-1.0",
                        error_message="upstream error",
                    ),
                    _record(
                        3106,
                        created_at=before_today,
                        status="success",
                        model="grok-imagine-1.0-video",
                    ),
                ]
            )
        )

        response = self.client.get("/v1/admin/call-logs", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        stats = payload["today_generation_stats"]
        self.assertEqual(stats["timezone"], "Asia/Shanghai")
        self.assertEqual(stats["date"], window["date"])
        self.assertEqual(stats["image_count"], 2)
        self.assertEqual(stats["video_count"], 1)

    def test_today_generation_stats_do_not_follow_current_filters(self):
        window = build_today_generation_window()
        inside_today = window["start_ms"] + 1_000
        asyncio.run(
            self._reset_records(
                [
                    _record(
                        3201,
                        created_at=inside_today,
                        status="success",
                        model="grok-imagine-1.0",
                    ),
                    _record(
                        3202,
                        created_at=inside_today + 1_000,
                        status="success",
                        model="grok-imagine-1.0-video",
                    ),
                ]
            )
        )

        response = self.client.get(
            "/v1/admin/call-logs?status=fail&model=grok-auto",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["summary"]["total_calls"], 0)
        self.assertEqual(payload["today_generation_stats"]["image_count"], 1)
        self.assertEqual(payload["today_generation_stats"]["video_count"], 1)

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
        self.assertIn("call-logs-", response.headers.get("content-disposition", ""))

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
