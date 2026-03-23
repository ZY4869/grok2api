from __future__ import annotations

import os
from typing import Any

from app.core.call_log_common import normalize_call_log_record
from app.core.storage import get_storage


def get_legacy_call_log_source_name(storage: Any | None = None) -> str:
    if storage is None:
        storage_type = os.getenv("SERVER_STORAGE_TYPE", "local").lower()
        if storage_type in ("mysql", "pgsql"):
            return storage_type
        return storage_type or "local"

    name = storage.__class__.__name__.lower()
    if "redis" in name:
        return "redis"
    if "sql" in name:
        return "sql"
    return "local"


async def load_legacy_call_logs() -> tuple[str, list[dict[str, Any]]]:
    storage = get_storage()
    source = get_legacy_call_log_source_name(storage)
    if hasattr(storage, "_read_call_logs"):
        records = await storage._read_call_logs()
    elif hasattr(storage, "_load_call_logs"):
        records = await storage._load_call_logs()
    else:
        return source, []

    return source, [normalize_call_log_record(item) for item in records if isinstance(item, dict)]


async def clear_legacy_call_logs() -> tuple[str, int]:
    storage = get_storage()
    source = get_legacy_call_log_source_name(storage)
    if not hasattr(storage, "clear_call_logs"):
        return source, 0
    deleted = await storage.clear_call_logs()
    return source, int(deleted or 0)

