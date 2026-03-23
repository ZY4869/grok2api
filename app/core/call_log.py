"""Request-scoped call log helpers."""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Optional

from fastapi import HTTPException

from app.core.config import get_config
from app.core.logger import logger
from app.core.call_log_store import get_call_log_store

_CALL_LOG_CLEANUP_INTERVAL_SEC = 60
_cleanup_lock = asyncio.Lock()
_last_cleanup_at = 0.0


@dataclass
class CallLogContext:
    trace_id: str
    api_type: str
    started_at: int
    model: str = ""
    token: str = ""
    pool: str = ""
    email: str = ""
    logged: bool = False


_call_log_context: contextvars.ContextVar[Optional[CallLogContext]] = (
    contextvars.ContextVar("call_log_context", default=None)
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def get_call_log_context() -> Optional[CallLogContext]:
    return _call_log_context.get()


def begin_call_log(
    api_type: str,
    *,
    trace_id: str = "",
    model: str = "",
    started_at: Optional[int] = None,
) -> CallLogContext:
    context = CallLogContext(
        trace_id=trace_id or uuid.uuid4().hex,
        api_type=str(api_type or "").strip(),
        started_at=int(started_at or _now_ms()),
        model=str(model or "").strip(),
    )
    _call_log_context.set(context)
    return context


def update_call_log_model(model: str) -> None:
    context = get_call_log_context()
    if context:
        context.model = str(model or "").strip()


def bind_call_log_account(token: str, pool: str = "", email: str = "") -> None:
    context = get_call_log_context()
    if not context:
        return
    context.token = str(token or "").strip()
    context.pool = str(pool or "").strip()
    context.email = str(email or "").strip()


def clear_call_log_context() -> None:
    _call_log_context.set(None)


async def _cleanup_if_due(force: bool = False) -> None:
    global _last_cleanup_at

    retention_days = int(get_config("call_log.retention_days", 0) or 0)
    if retention_days <= 0:
        return

    now = time.monotonic()
    if not force and (now - _last_cleanup_at) < _CALL_LOG_CLEANUP_INTERVAL_SEC:
        return

    async with _cleanup_lock:
        now = time.monotonic()
        if not force and (now - _last_cleanup_at) < _CALL_LOG_CLEANUP_INTERVAL_SEC:
            return
        try:
            await get_call_log_store().cleanup_call_logs(retention_days)
            _last_cleanup_at = now
        except Exception as exc:
            logger.warning(f"Call log cleanup skipped: {exc}")


def _build_record(
    context: CallLogContext,
    *,
    status: str,
    error_code: str = "",
    error_message: str = "",
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "created_at": _now_ms(),
        "status": status,
        "api_type": context.api_type,
        "model": context.model,
        "email": context.email,
        "token": context.token,
        "pool": context.pool,
        "duration_ms": max(0, _now_ms() - int(context.started_at or _now_ms())),
        "trace_id": context.trace_id,
        "error_code": error_code,
        "error_message": error_message,
    }


def _extract_error_details(error: Any) -> tuple[str, str]:
    if isinstance(error, HTTPException):
        return f"http_{error.status_code}", str(error.detail or "http_error")
    details = getattr(error, "details", None)
    message = getattr(error, "message", None)
    code = getattr(error, "code", None)
    error_type = getattr(error, "error_type", None)
    if isinstance(details, dict):
        error_code = details.get("error_code") or code or "upstream_error"
        text = message or str(details.get("error") or error)
        return str(error_code), str(text or "upstream_error")
    if message is not None or code is not None or error_type is not None:
        return str(code or error_type or "app_error"), str(message or "app_error")
    message = str(error or "").strip()
    return "internal_error", message or "internal_error"


async def _append_record(record: dict[str, Any]) -> None:
    await _cleanup_if_due(force=False)
    await get_call_log_store().append_call_log(record)


async def log_call_success() -> None:
    context = get_call_log_context()
    if not context or context.logged:
        clear_call_log_context()
        return

    try:
        await _append_record(_build_record(context, status="success"))
        context.logged = True
    except Exception as exc:
        logger.warning(f"Call log success write failed: {exc}")
    finally:
        clear_call_log_context()


async def log_call_failure(
    error: Any = None,
    *,
    error_code: str = "",
    error_message: str = "",
) -> None:
    context = get_call_log_context()
    if not context or context.logged:
        clear_call_log_context()
        return

    if error is not None:
        error_code, error_message = _extract_error_details(error)

    try:
        await _append_record(
            _build_record(
                context,
                status="fail",
                error_code=str(error_code or "internal_error"),
                error_message=str(error_message or "internal_error"),
            )
        )
        context.logged = True
    except Exception as exc:
        logger.warning(f"Call log failure write failed: {exc}")
    finally:
        clear_call_log_context()


async def wrap_call_log_stream(stream):
    try:
        async for chunk in stream:
            yield chunk
    except Exception as error:
        await log_call_failure(error)
        raise


__all__ = [
    "CallLogContext",
    "begin_call_log",
    "bind_call_log_account",
    "clear_call_log_context",
    "get_call_log_context",
    "log_call_failure",
    "log_call_success",
    "update_call_log_model",
    "wrap_call_log_stream",
]
