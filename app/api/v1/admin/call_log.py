from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import verify_app_key
from app.core.config import get_config
from app.core.storage import get_storage

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


@router.get("/call-logs", dependencies=[Depends(verify_app_key)])
async def get_call_logs(
    status: str = Query(""),
    api_type: str = Query(""),
    model: str = Query(""),
    account_keyword: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    storage = get_storage()
    retention_days = int(get_config("call_log.retention_days", 0) or 0)
    await storage.cleanup_call_logs(retention_days)
    return await storage.query_call_logs(
        {
            "status": status,
            "api_type": api_type,
            "model": model,
            "account_keyword": account_keyword,
            "date_from": _parse_date_value(date_from, end_of_day=False),
            "date_to": _parse_date_value(date_to, end_of_day=True),
            "page": page,
            "page_size": page_size,
        }
    )


@router.delete("/call-logs", dependencies=[Depends(verify_app_key)])
async def clear_call_logs():
    storage = get_storage()
    deleted = await storage.clear_call_logs()
    return {"status": "success", "deleted": deleted}


__all__ = ["router"]
