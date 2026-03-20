import tempfile
import time
import unittest
from pathlib import Path

from app.api.v1.chat import ChatCompletionRequest, MessageItem, validate_request
from app.core import storage as storage_module
from app.core.storage import _build_call_log_response


def _log_record(
    *,
    record_id: str,
    created_at: int,
    status: str,
    token: str = "",
    pool: str = "",
    email: str = "",
    duration_ms: int = 0,
    api_type: str = "chat.completions",
    model: str = "grok-3",
) -> dict:
    return {
        "id": record_id,
        "created_at": created_at,
        "status": status,
        "api_type": api_type,
        "model": model,
        "email": email,
        "token": token,
        "pool": pool,
        "duration_ms": duration_ms,
        "trace_id": f"trace-{record_id}",
        "error_code": "" if status == "success" else "internal_error",
        "error_message": "" if status == "success" else "failed",
    }


class CallLogResponseTests(unittest.TestCase):
    def test_build_call_log_response_supports_filters_and_account_aggregation(self):
        now = int(time.time() * 1000)
        records = [
            _log_record(
                record_id="1",
                created_at=now - 1000,
                status="success",
                token="token-1",
                pool="ssoBasic",
                email="user1@example.com",
                duration_ms=120,
            ),
            _log_record(
                record_id="2",
                created_at=now - 500,
                status="fail",
                token="token-1",
                pool="ssoBasic",
                email="user1@example.com",
                duration_ms=240,
            ),
            _log_record(
                record_id="3",
                created_at=now - 200,
                status="success",
                token="token-2",
                pool="ssoSuper",
                email="user2@example.com",
                duration_ms=60,
            ),
        ]

        result = _build_call_log_response(records, {"page": 1, "page_size": 50})

        self.assertEqual(result["summary"]["total_calls"], 3)
        self.assertEqual(result["summary"]["success_count"], 2)
        self.assertEqual(result["summary"]["fail_count"], 1)
        self.assertEqual(len(result["accounts"]), 2)
        self.assertEqual(result["accounts"][0]["token"], "token-1")
        self.assertEqual(result["accounts"][0]["call_count"], 2)
        self.assertEqual(result["accounts"][0]["fail_count"], 1)
        self.assertEqual(result["accounts"][0]["avg_duration_ms"], 180)

        filtered = _build_call_log_response(
            records,
            {
                "account_keyword": "user2@example.com",
                "status": "success",
                "page": 1,
                "page_size": 50,
            },
        )
        self.assertEqual(filtered["summary"]["total_calls"], 1)
        self.assertEqual(filtered["items"][0]["token"], "token-2")


class LocalStorageCallLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_storage_call_log_lifecycle(self):
        original_call_log_file = storage_module.CALL_LOG_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                storage_module.CALL_LOG_FILE = Path(temp_dir) / "call_logs.jsonl"
                storage = storage_module.LocalStorage()
                now = int(time.time() * 1000)

                await storage.append_call_log(
                    _log_record(
                        record_id="old",
                        created_at=now - 10 * 24 * 3600 * 1000,
                        status="fail",
                        token="expired-token",
                        pool="ssoBasic",
                        duration_ms=500,
                    )
                )
                await storage.append_call_log(
                    _log_record(
                        record_id="new",
                        created_at=now,
                        status="success",
                        token="live-token",
                        pool="ssoSuper",
                        email="live@example.com",
                        duration_ms=90,
                    )
                )

                queried = await storage.query_call_logs({"page": 1, "page_size": 50})
                self.assertEqual(queried["summary"]["total_calls"], 2)
                self.assertEqual(len(queried["items"]), 2)

                removed = await storage.cleanup_call_logs(7)
                self.assertEqual(removed, 1)

                after_cleanup = await storage.query_call_logs({"page": 1, "page_size": 50})
                self.assertEqual(after_cleanup["summary"]["total_calls"], 1)
                self.assertEqual(after_cleanup["items"][0]["token"], "live-token")

                cleared = await storage.clear_call_logs()
                self.assertEqual(cleared, 1)

                after_clear = await storage.query_call_logs({"page": 1, "page_size": 50})
                self.assertEqual(after_clear["summary"]["total_calls"], 0)
        finally:
            storage_module.CALL_LOG_FILE = original_call_log_file


class ChatValidationTests(unittest.TestCase):
    def test_validate_request_supports_image_models_without_name_error(self):
        request = ChatCompletionRequest(
            model="grok-imagine-1.0",
            messages=[MessageItem(role="user", content="draw a cat")],
        )

        validate_request(request)

        self.assertIsNotNone(request.image_config)
        self.assertEqual(request.image_config.n, 1)


if __name__ == "__main__":
    unittest.main()
