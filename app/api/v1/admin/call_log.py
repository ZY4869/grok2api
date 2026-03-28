from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.core.auth import verify_app_key
from app.core.call_log_admin import (
    build_account_keyword_tokens,
    build_token_snapshot,
    refresh_admin_token_manager,
)
from app.core.call_log_common import DEFAULT_CALL_LOG_PAGE_SIZE
from app.core.call_log_store import get_call_log_store
from app.core.config import get_config
from app.core.logger import logger
from app.services.token.manager import get_token_manager

router = APIRouter()


def _parse_date_value(value: Optional[str], *, end_of_day: bool = False) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)

    try:
        if len(raw) <= 10:
            dt = datetime.fromisoformat(raw)
            if end_of_day:
                dt = dt + timedelta(days=1) - timedelta(milliseconds=1)
            return int(dt.timestamp() * 1000)

        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return int(dt.timestamp() * 1000)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {raw}") from exc


def _build_filters(
    *,
    status: str = "",
    api_type: str = "",
    model: str = "",
    account_keyword: str = "",
    account_tokens: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    page_size: int = DEFAULT_CALL_LOG_PAGE_SIZE,
) -> dict[str, Any]:
    return {
        "status": status,
        "api_type": api_type,
        "model": model,
        "account_keyword": account_keyword,
        "account_tokens": account_tokens or [],
        "date_from": _parse_date_value(date_from, end_of_day=False),
        "date_to": _parse_date_value(date_to, end_of_day=True),
        "page": page,
        "page_size": page_size,
    }


def _log_extra(request: Request, operation: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "traceID": getattr(request.state, "trace_id", ""),
        "operation": operation,
    }
    payload.update(extra)
    return payload


@router.get("/call-logs", dependencies=[Depends(verify_app_key)])
async def get_call_logs(
    request: Request,
    status: str = Query(""),
    api_type: str = Query(""),
    model: str = Query(""),
    account_keyword: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_CALL_LOG_PAGE_SIZE, ge=1, le=200),
):
    store = get_call_log_store()
    token_mgr = await get_token_manager()
    await refresh_admin_token_manager(token_mgr)
    token_snapshot = build_token_snapshot(token_mgr)
    account_tokens = build_account_keyword_tokens(account_keyword, token_snapshot)
    retention_days = int(get_config("call_log.retention_days", 0) or 0)
    await store.cleanup_call_logs(retention_days)
    filters = _build_filters(
        status=status,
        api_type=api_type,
        model=model,
        account_keyword=account_keyword,
        account_tokens=account_tokens,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    response = await store.query_call_logs(filters, token_snapshot=token_snapshot)
    migration_status = response.get("migration_status") or {}
    if str(migration_status.get("state") or "") == "failed":
        logger.warning(
            "Call log migration status is failed",
            extra=_log_extra(
                request,
                "call_log_query",
                migration_state=migration_status.get("state"),
            ),
        )
    return response


@router.get("/call-logs/export", dependencies=[Depends(verify_app_key)])
async def export_call_logs(
    request: Request,
    status: str = Query(""),
    api_type: str = Query(""),
    model: str = Query(""),
    account_keyword: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
):
    store = get_call_log_store()
    token_mgr = await get_token_manager()
    await refresh_admin_token_manager(token_mgr)
    token_snapshot = build_token_snapshot(token_mgr)
    account_tokens = build_account_keyword_tokens(account_keyword, token_snapshot)
    retention_days = int(get_config("call_log.retention_days", 0) or 0)
    await store.cleanup_call_logs(retention_days)
    filters = _build_filters(
        status=status,
        api_type=api_type,
        model=model,
        account_keyword=account_keyword,
        account_tokens=account_tokens,
        date_from=date_from,
        date_to=date_to,
    )
    total_records = await store.count_call_logs(filters)
    started_at = datetime.utcnow()
    logger.info(
        "Call log export started",
        extra=_log_extra(
            request,
            "call_log_export",
            record_count=total_records,
            filter_status=status or "",
            filter_api_type=api_type or "",
            filter_model=model or "",
            filter_date_from=date_from or "",
            filter_date_to=date_to or "",
            account_keyword_present=bool(account_keyword),
        ),
    )

    async def stream_csv():
        try:
            async for chunk in store.iter_csv_export(filters, token_snapshot=token_snapshot):
                yield chunk
            logger.info(
                "Call log export completed",
                extra=_log_extra(
                    request,
                    "call_log_export",
                    record_count=total_records,
                    duration_ms=round(
                        (datetime.utcnow() - started_at).total_seconds() * 1000, 2
                    ),
                ),
            )
        except Exception as exc:
            logger.error(
                f"Call log export failed: {exc}",
                extra=_log_extra(
                    request,
                    "call_log_export",
                    record_count=total_records,
                    duration_ms=round(
                        (datetime.utcnow() - started_at).total_seconds() * 1000, 2
                    ),
                ),
            )
            raise

    filename = f"call-logs-{started_at.strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        stream_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/call-logs", dependencies=[Depends(verify_app_key)])
async def clear_call_logs(request: Request):
    store = get_call_log_store()
    started_at = datetime.utcnow()
    logger.info(
        "Call log clear started",
        extra=_log_extra(request, "call_log_clear"),
    )
    try:
        result = await store.clear_with_legacy()
        logger.info(
            "Call log clear completed",
            extra=_log_extra(
                request,
                "call_log_clear",
                deleted=result.get("deleted", 0),
                deleted_current=result.get("deleted_current", 0),
                deleted_legacy=result.get("deleted_legacy", 0),
                duration_ms=round(
                    (datetime.utcnow() - started_at).total_seconds() * 1000, 2
                ),
            ),
        )
        return {"status": "success", **result}
    except Exception as exc:
        logger.error(
            f"Call log clear failed: {exc}",
            extra=_log_extra(
                request,
                "call_log_clear",
                duration_ms=round(
                    (datetime.utcnow() - started_at).total_seconds() * 1000, 2
                ),
            ),
        )
        raise


__all__ = ["router"]
