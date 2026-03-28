from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CALL_LOG_EXPORT_HEADERS = [
    "created_at",
    "status",
    "api_type",
    "model",
    "email",
    "token",
    "pool",
    "duration_ms",
    "trace_id",
    "error_code",
    "error_message",
]

CALL_LOG_ACCOUNT_RANK_LIMIT = 20
DEFAULT_CALL_LOG_PAGE_SIZE = 100
MAX_CALL_LOG_PAGE_SIZE = 200


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def normalize_call_log_record(record: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(record or {})
    return {
        "id": str(payload.get("id") or "").strip(),
        "created_at": coerce_int(payload.get("created_at")),
        "status": str(payload.get("status") or "").strip().lower() or "fail",
        "api_type": str(payload.get("api_type") or "").strip(),
        "model": str(payload.get("model") or "").strip(),
        "email": str(payload.get("email") or "").strip(),
        "token": str(payload.get("token") or "").strip(),
        "pool": str(payload.get("pool") or "").strip(),
        "duration_ms": max(0, coerce_int(payload.get("duration_ms"))),
        "trace_id": str(payload.get("trace_id") or "").strip(),
        "error_code": str(payload.get("error_code") or "").strip(),
        "error_message": str(payload.get("error_message") or "").strip(),
    }


@dataclass(frozen=True)
class CallLogFilters:
    status: str = ""
    api_type: str = ""
    model: str = ""
    account_keyword: str = ""
    account_tokens: tuple[str, ...] = ()
    date_from: int = 0
    date_to: int = 0
    page: int = 1
    page_size: int = DEFAULT_CALL_LOG_PAGE_SIZE


def build_call_log_filters(filters: dict[str, Any] | None = None) -> CallLogFilters:
    payload = dict(filters or {})
    account_tokens = payload.get("account_tokens") or []
    if not isinstance(account_tokens, (list, tuple, set)):
        account_tokens = []
    normalized_tokens: list[str] = []
    for token in account_tokens:
        value = str(token or "").replace("sso=", "").strip()
        if value and value not in normalized_tokens:
            normalized_tokens.append(value)
    page = max(1, coerce_int(payload.get("page"), 1))
    page_size = coerce_int(payload.get("page_size"), DEFAULT_CALL_LOG_PAGE_SIZE)
    if page_size <= 0:
        page_size = DEFAULT_CALL_LOG_PAGE_SIZE
    page_size = min(page_size, MAX_CALL_LOG_PAGE_SIZE)
    return CallLogFilters(
        status=str(payload.get("status") or "").strip().lower(),
        api_type=str(payload.get("api_type") or "").strip(),
        model=str(payload.get("model") or "").strip(),
        account_keyword=str(payload.get("account_keyword") or "").strip(),
        account_tokens=tuple(normalized_tokens),
        date_from=max(0, coerce_int(payload.get("date_from"), 0)),
        date_to=max(0, coerce_int(payload.get("date_to"), 0)),
        page=page,
        page_size=page_size,
    )


def build_call_log_response(
    *,
    items: list[dict[str, Any]],
    summary: dict[str, Any],
    accounts: list[dict[str, Any]],
    account_stats: dict[str, Any] | None = None,
    quick_image_limit_stats: dict[str, Any] | None = None,
    today_generation_stats: dict[str, Any] | None = None,
    page: int,
    page_size: int,
    migration_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total_items = max(0, coerce_int(summary.get("total_calls"), 0))
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    current_page = min(max(1, page), total_pages)
    return {
        "items": items,
        "summary": {
            "total_calls": total_items,
            "success_count": max(0, coerce_int(summary.get("success_count"), 0)),
            "fail_count": max(0, coerce_int(summary.get("fail_count"), 0)),
            "avg_duration_ms": round(coerce_float(summary.get("avg_duration_ms"), 0.0), 2),
            "unique_accounts": max(0, coerce_int(summary.get("unique_accounts"), 0)),
        },
        "accounts": accounts,
        "account_stats": account_stats
        or {
            "total_accounts": 0,
            "available_accounts": 0,
            "limit_accounts": 0,
            "called_accounts": 0,
        },
        "quick_image_limit_stats": quick_image_limit_stats
        or {
            "total_hits": 0,
            "unique_accounts": 0,
            "items": [],
        },
        "today_generation_stats": today_generation_stats
        or {
            "timezone": "Asia/Shanghai",
            "date": "",
            "image_count": 0,
            "video_count": 0,
        },
        "pagination": {
            "page": current_page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
        },
        "migration_status": migration_status or {
            "state": "pending",
            "source": "",
            "message": "",
            "migrated_count": 0,
        },
    }
