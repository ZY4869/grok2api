from __future__ import annotations

import asyncio
import csv
import io
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

import orjson
from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    create_engine,
    delete,
    desc,
    func,
    insert,
    or_,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.call_log_common import (
    CALL_LOG_ACCOUNT_RANK_LIMIT,
    CALL_LOG_EXPORT_HEADERS,
    CallLogFilters,
    build_call_log_filters,
    build_call_log_response,
    coerce_float,
    coerce_int,
    normalize_call_log_record,
)
from app.core.call_log_legacy import clear_legacy_call_logs, get_legacy_call_log_source_name, load_legacy_call_logs
from app.core.logger import logger
from app.core.storage import DATA_DIR, StorageFactory

CALL_LOG_META_KEY = "legacy_migration_status"

metadata = MetaData()

call_log_entries = Table(
    "call_log_entries",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("created_at", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("api_type", String(128), nullable=False, default=""),
    Column("model", String(255), nullable=False, default=""),
    Column("email", String(255), nullable=False, default=""),
    Column("token", String(512), nullable=False, default=""),
    Column("pool", String(64), nullable=False, default=""),
    Column("duration_ms", BigInteger, nullable=False, default=0),
    Column("trace_id", String(128), nullable=False, default=""),
    Column("error_code", String(128), nullable=False, default=""),
    Column("error_message", Text, nullable=False, default=""),
)

call_log_meta = Table(
    "call_log_meta",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False, default=""),
    Column("updated_at", BigInteger, nullable=False, default=0),
)

Index("idx_call_log_entries_created_at", call_log_entries.c.created_at)
Index("idx_call_log_entries_status", call_log_entries.c.status)
Index("idx_call_log_entries_api_type", call_log_entries.c.api_type)
Index("idx_call_log_entries_model", call_log_entries.c.model)
Index("idx_call_log_entries_token", call_log_entries.c.token)
Index("idx_call_log_entries_pool", call_log_entries.c.pool)
Index("idx_call_log_entries_email", call_log_entries.c.email)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _build_sqlite_url(path: str | Path | None = None) -> tuple[str, Path]:
    raw_path = Path(path or (DATA_DIR / "call_logs.db")).expanduser()
    resolved = raw_path if raw_path.is_absolute() else (Path.cwd() / raw_path).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{resolved.as_posix()}", resolved


def _detect_sql_storage_type(url: str) -> str:
    prefix = url.split("://", 1)[0].split("+", 1)[0].lower()
    if prefix.startswith("mysql"):
        return "mysql"
    if prefix.startswith("postgresql") or prefix.startswith("pgsql") or prefix.startswith("postgres"):
        return "pgsql"
    return ""


def _resolve_store_target() -> dict[str, Any]:
    env_url = str(os.getenv("CALL_LOG_DB_URL", "") or "").strip()
    if env_url:
        if env_url.startswith("sqlite:///"):
            sqlite_url, sqlite_path = _build_sqlite_url(env_url.removeprefix("sqlite:///"))
            return {"backend": "sqlite", "url": sqlite_url, "path": sqlite_path}

        storage_type = _detect_sql_storage_type(env_url)
        if storage_type:
            url, connect_args = StorageFactory._prepare_sql_url_and_connect_args(
                storage_type, env_url
            )
            return {"backend": "sql", "url": url, "connect_args": connect_args}

        sqlite_url, sqlite_path = _build_sqlite_url(env_url)
        return {"backend": "sqlite", "url": sqlite_url, "path": sqlite_path}

    storage_type = os.getenv("SERVER_STORAGE_TYPE", "local").lower()
    storage_url = str(os.getenv("SERVER_STORAGE_URL", "") or "").strip()
    if storage_type in ("mysql", "pgsql") and storage_url:
        url, connect_args = StorageFactory._prepare_sql_url_and_connect_args(
            storage_type, storage_url
        )
        return {"backend": "sql", "url": url, "connect_args": connect_args}

    sqlite_url, sqlite_path = _build_sqlite_url()
    return {"backend": "sqlite", "url": sqlite_url, "path": sqlite_path}


class CallLogStore:
    def __init__(self, target: dict[str, Any]):
        self._target = target
        self._backend = target["backend"]
        self._prepare_lock = asyncio.Lock()
        self._prepared = False
        if self._backend == "sqlite":
            self._sync_engine: Engine | None = create_engine(
                target["url"], connect_args={"check_same_thread": False}
            )
            self._async_engine: AsyncEngine | None = None
        else:
            self._sync_engine = None
            self._async_engine = create_async_engine(
                target["url"],
                echo=False,
                pool_pre_ping=True,
                **({"connect_args": target.get("connect_args")} if target.get("connect_args") else {}),
            )

    async def prepare(self) -> None:
        if self._prepared:
            return
        async with self._prepare_lock:
            if self._prepared:
                return
            await self._ensure_schema()
            await self._maybe_migrate_legacy()
            self._prepared = True

    async def append_call_log(self, record: dict[str, Any]) -> None:
        await self.prepare()
        payload = normalize_call_log_record(record)
        if self._backend == "sqlite":
            await asyncio.to_thread(self._append_sync, payload)
            return
        async with self._async_engine.begin() as conn:
            await conn.execute(insert(call_log_entries), [payload])

    async def query_call_logs(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.prepare()
        parsed = build_call_log_filters(filters)
        summary = await self._fetch_summary(parsed)
        total_items = coerce_int(summary.get("total_calls"), 0)
        total_pages = max(1, (total_items + parsed.page_size - 1) // parsed.page_size)
        current_page = min(max(1, parsed.page), total_pages)
        items = await self._fetch_items(parsed, current_page)
        accounts = await self._fetch_accounts(parsed)
        migration_status = await self.get_migration_status()
        return build_call_log_response(
            items=items,
            summary=summary,
            accounts=accounts,
            page=current_page,
            page_size=parsed.page_size,
            migration_status=migration_status,
        )

    async def count_call_logs(self, filters: dict[str, Any] | None = None) -> int:
        summary = await self._fetch_summary(build_call_log_filters(filters))
        return coerce_int(summary.get("total_calls"), 0)

    async def iter_csv_export(
        self, filters: dict[str, Any] | None = None, batch_size: int = 1000
    ) -> AsyncIterator[bytes]:
        await self.prepare()
        parsed = build_call_log_filters(filters)
        yield self._render_csv_chunk([CALL_LOG_EXPORT_HEADERS], with_bom=True)
        offset = 0
        while True:
            rows = await self._fetch_export_rows(parsed, offset, batch_size)
            if not rows:
                break
            payload = [
                [row.get(column, "") for column in CALL_LOG_EXPORT_HEADERS] for row in rows
            ]
            yield self._render_csv_chunk(payload)
            offset += len(rows)

    async def cleanup_call_logs(self, retention_days: int) -> int:
        await self.prepare()
        retention_days = coerce_int(retention_days, 0)
        if retention_days <= 0:
            return 0
        cutoff_ms = _now_ms() - retention_days * 24 * 3600 * 1000
        statement = delete(call_log_entries).where(call_log_entries.c.created_at < cutoff_ms)
        if self._backend == "sqlite":
            return await asyncio.to_thread(self._delete_sync, statement)
        async with self._async_engine.begin() as conn:
            result = await conn.execute(statement)
        return int(result.rowcount or 0)

    async def clear_call_logs(self) -> int:
        await self.prepare()
        count = await self.count_call_logs({})
        statement = delete(call_log_entries)
        if self._backend == "sqlite":
            await asyncio.to_thread(self._delete_sync, statement)
        else:
            async with self._async_engine.begin() as conn:
                await conn.execute(statement)
        return count

    async def clear_with_legacy(self) -> dict[str, int | str]:
        deleted_current = await self.clear_call_logs()
        deleted_legacy = 0
        legacy_source = get_legacy_call_log_source_name()
        try:
            legacy_source, deleted_legacy = await clear_legacy_call_logs()
            if deleted_legacy > 0:
                await self._set_meta(
                    {
                        "state": "cleared",
                        "source": legacy_source,
                        "message": "legacy logs cleared manually",
                        "migrated_count": 0,
                    }
                )
        except Exception as exc:
            logger.warning(f"Call log legacy clear skipped: {exc}")
        return {
            "deleted": deleted_current + deleted_legacy,
            "deleted_current": deleted_current,
            "deleted_legacy": deleted_legacy,
            "legacy_source": legacy_source,
        }

    async def get_migration_status(self) -> dict[str, Any]:
        await self.prepare()
        payload = await self._get_meta()
        if payload:
            return payload
        return {
            "state": "pending",
            "source": get_legacy_call_log_source_name(),
            "message": "",
            "migrated_count": 0,
        }

    async def close(self) -> None:
        if self._async_engine is not None:
            await self._async_engine.dispose()
        if self._sync_engine is not None:
            await asyncio.to_thread(self._sync_engine.dispose)

    async def _maybe_migrate_legacy(self) -> None:
        existing_status = await self._get_meta()
        if existing_status and existing_status.get("state") in {
            "completed",
            "skipped",
            "failed",
            "cleared",
        }:
            return

        source = get_legacy_call_log_source_name()
        started_at = time.monotonic()
        logger.info(
            "Call log migration started",
            extra={"source": source, "operation": "call_log_migration"},
        )
        try:
            source, records = await load_legacy_call_logs()
            if not records:
                await self._set_meta(
                    {
                        "state": "skipped",
                        "source": source,
                        "message": "no legacy call logs found",
                        "migrated_count": 0,
                    }
                )
                logger.info(
                    "Call log migration skipped",
                    extra={"source": source, "operation": "call_log_migration", "record_count": 0},
                )
                return

            await self._import_records(records)
            duration_ms = round((time.monotonic() - started_at) * 1000, 2)
            await self._set_meta(
                {
                    "state": "completed",
                    "source": source,
                    "message": "",
                    "migrated_count": len(records),
                }
            )
            logger.info(
                "Call log migration completed",
                extra={
                    "source": source,
                    "operation": "call_log_migration",
                    "record_count": len(records),
                    "duration_ms": duration_ms,
                },
            )
        except Exception as exc:
            duration_ms = round((time.monotonic() - started_at) * 1000, 2)
            await self._set_meta(
                {
                    "state": "failed",
                    "source": source,
                    "message": str(exc),
                    "migrated_count": 0,
                }
            )
            logger.warning(
                f"Call log migration failed: {exc}",
                extra={
                    "source": source,
                    "operation": "call_log_migration",
                    "duration_ms": duration_ms,
                },
            )

    async def _fetch_summary(self, filters: CallLogFilters) -> dict[str, Any]:
        conditions = self._build_conditions(filters)
        summary_stmt = select(
            func.count().label("total_calls"),
            func.coalesce(
                func.sum(case((call_log_entries.c.status == "success", 1), else_=0)), 0
            ).label("success_count"),
            func.coalesce(
                func.sum(case((call_log_entries.c.status == "fail", 1), else_=0)), 0
            ).label("fail_count"),
            func.coalesce(func.avg(call_log_entries.c.duration_ms), 0).label("avg_duration_ms"),
        )
        if conditions:
            summary_stmt = summary_stmt.where(and_(*conditions))

        account_scope = select(call_log_entries.c.token, call_log_entries.c.pool)
        if conditions:
            account_scope = account_scope.where(and_(*conditions))
        account_scope = account_scope.group_by(call_log_entries.c.token, call_log_entries.c.pool).subquery()
        unique_stmt = select(func.count()).select_from(account_scope)

        summary_row, unique_accounts = await self._fetch_summary_rows(summary_stmt, unique_stmt)
        return {
            "total_calls": coerce_int(getattr(summary_row, "total_calls", 0), 0),
            "success_count": coerce_int(getattr(summary_row, "success_count", 0), 0),
            "fail_count": coerce_int(getattr(summary_row, "fail_count", 0), 0),
            "avg_duration_ms": round(
                coerce_float(getattr(summary_row, "avg_duration_ms", 0.0), 0.0), 2
            ),
            "unique_accounts": coerce_int(unique_accounts, 0),
        }

    async def _fetch_accounts(self, filters: CallLogFilters) -> list[dict[str, Any]]:
        conditions = self._build_conditions(filters)
        call_count = func.count().label("call_count")
        last_called_at = func.max(call_log_entries.c.created_at).label("last_called_at")
        stmt = select(
            func.max(call_log_entries.c.email).label("email"),
            call_log_entries.c.token,
            call_log_entries.c.pool,
            call_count,
            func.coalesce(
                func.sum(case((call_log_entries.c.status == "success", 1), else_=0)), 0
            ).label("success_count"),
            func.coalesce(
                func.sum(case((call_log_entries.c.status == "fail", 1), else_=0)), 0
            ).label("fail_count"),
            func.coalesce(func.avg(call_log_entries.c.duration_ms), 0).label("avg_duration_ms"),
            last_called_at,
        ).group_by(call_log_entries.c.token, call_log_entries.c.pool)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(desc(call_count), desc(last_called_at)).limit(CALL_LOG_ACCOUNT_RANK_LIMIT)
        rows = await self._fetch_rows(stmt)
        accounts: list[dict[str, Any]] = []
        for row in rows:
            accounts.append(
                {
                    "email": row.email or "",
                    "token": row.token or "",
                    "pool": row.pool or "",
                    "call_count": coerce_int(row.call_count, 0),
                    "success_count": coerce_int(row.success_count, 0),
                    "fail_count": coerce_int(row.fail_count, 0),
                    "avg_duration_ms": round(coerce_float(row.avg_duration_ms, 0.0), 2),
                    "last_called_at": coerce_int(row.last_called_at, 0),
                }
            )
        return accounts

    async def _fetch_items(self, filters: CallLogFilters, page: int) -> list[dict[str, Any]]:
        conditions = self._build_conditions(filters)
        stmt = select(call_log_entries)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(
            desc(call_log_entries.c.created_at), desc(call_log_entries.c.id)
        ).limit(filters.page_size).offset((page - 1) * filters.page_size)
        rows = await self._fetch_rows(stmt)
        return [normalize_call_log_record(dict(row._mapping)) for row in rows]

    async def _fetch_export_rows(
        self, filters: CallLogFilters, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        conditions = self._build_conditions(filters)
        stmt = select(
            call_log_entries.c.created_at,
            call_log_entries.c.status,
            call_log_entries.c.api_type,
            call_log_entries.c.model,
            call_log_entries.c.email,
            call_log_entries.c.token,
            call_log_entries.c.pool,
            call_log_entries.c.duration_ms,
            call_log_entries.c.trace_id,
            call_log_entries.c.error_code,
            call_log_entries.c.error_message,
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(
            desc(call_log_entries.c.created_at), desc(call_log_entries.c.id)
        ).limit(limit).offset(offset)
        rows = await self._fetch_rows(stmt)
        return [dict(row._mapping) for row in rows]

    def _build_conditions(self, filters: CallLogFilters) -> list[Any]:
        conditions: list[Any] = []
        if filters.date_from:
            conditions.append(call_log_entries.c.created_at >= filters.date_from)
        if filters.date_to:
            conditions.append(call_log_entries.c.created_at <= filters.date_to)
        if filters.status and filters.status not in ("all", "any"):
            conditions.append(call_log_entries.c.status == filters.status)
        if filters.api_type:
            needle = f"%{filters.api_type.lower()}%"
            conditions.append(func.lower(call_log_entries.c.api_type).like(needle))
        if filters.model:
            needle = f"%{filters.model.lower()}%"
            conditions.append(func.lower(call_log_entries.c.model).like(needle))
        if filters.account_keyword:
            needle = f"%{filters.account_keyword.lower()}%"
            conditions.append(
                or_(
                    func.lower(func.coalesce(call_log_entries.c.email, "")).like(needle),
                    func.lower(func.coalesce(call_log_entries.c.token, "")).like(needle),
                    func.lower(func.coalesce(call_log_entries.c.pool, "")).like(needle),
                )
            )
        return conditions

    async def _fetch_summary_rows(self, summary_stmt: Any, unique_stmt: Any) -> tuple[Any, Any]:
        if self._backend == "sqlite":
            return await asyncio.to_thread(self._fetch_summary_rows_sync, summary_stmt, unique_stmt)
        async with self._async_engine.connect() as conn:
            summary_row = (await conn.execute(summary_stmt)).one()
            unique_accounts = (await conn.execute(unique_stmt)).scalar_one()
        return summary_row, unique_accounts

    async def _fetch_rows(self, stmt: Any) -> list[Any]:
        if self._backend == "sqlite":
            return await asyncio.to_thread(self._fetch_rows_sync, stmt)
        async with self._async_engine.connect() as conn:
            return list((await conn.execute(stmt)).fetchall())

    async def _get_meta(self) -> dict[str, Any] | None:
        stmt = select(call_log_meta.c.value).where(call_log_meta.c.key == CALL_LOG_META_KEY)
        if self._backend == "sqlite":
            raw_value = await asyncio.to_thread(self._fetch_meta_sync, stmt)
        else:
            async with self._async_engine.connect() as conn:
                raw_value = (await conn.execute(stmt)).scalar_one_or_none()
        if not raw_value:
            return None
        try:
            return orjson.loads(raw_value)
        except Exception:
            return None

    async def _set_meta(self, payload: dict[str, Any]) -> None:
        value = orjson.dumps(
            {
                "state": str(payload.get("state") or "pending"),
                "source": str(payload.get("source") or ""),
                "message": str(payload.get("message") or ""),
                "migrated_count": coerce_int(payload.get("migrated_count"), 0),
                "updated_at": _now_ms(),
            }
        ).decode("utf-8")
        if self._backend == "sqlite":
            await asyncio.to_thread(self._set_meta_sync, value)
            return
        async with self._async_engine.begin() as conn:
            await conn.execute(delete(call_log_meta).where(call_log_meta.c.key == CALL_LOG_META_KEY))
            await conn.execute(
                insert(call_log_meta),
                [{"key": CALL_LOG_META_KEY, "value": value, "updated_at": _now_ms()}],
            )

    async def _import_records(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        if self._backend == "sqlite":
            await asyncio.to_thread(self._import_records_sync, records)
            return
        async with self._async_engine.begin() as conn:
            await conn.execute(insert(call_log_entries), records)

    async def _ensure_schema(self) -> None:
        if self._backend == "sqlite":
            await asyncio.to_thread(metadata.create_all, self._sync_engine)
            return
        async with self._async_engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    def _append_sync(self, payload: dict[str, Any]) -> None:
        with self._sync_engine.begin() as conn:
            conn.execute(insert(call_log_entries), [payload])

    def _delete_sync(self, statement: Any) -> int:
        with self._sync_engine.begin() as conn:
            result = conn.execute(statement)
        return int(result.rowcount or 0)

    def _fetch_rows_sync(self, stmt: Any) -> list[Any]:
        with self._sync_engine.connect() as conn:
            return list(conn.execute(stmt).fetchall())

    def _fetch_summary_rows_sync(self, summary_stmt: Any, unique_stmt: Any) -> tuple[Any, Any]:
        with self._sync_engine.connect() as conn:
            summary_row = conn.execute(summary_stmt).one()
            unique_accounts = conn.execute(unique_stmt).scalar_one()
        return summary_row, unique_accounts

    def _fetch_meta_sync(self, stmt: Any) -> str | None:
        with self._sync_engine.connect() as conn:
            return conn.execute(stmt).scalar_one_or_none()

    def _set_meta_sync(self, value: str) -> None:
        with self._sync_engine.begin() as conn:
            conn.execute(delete(call_log_meta).where(call_log_meta.c.key == CALL_LOG_META_KEY))
            conn.execute(
                insert(call_log_meta),
                [{"key": CALL_LOG_META_KEY, "value": value, "updated_at": _now_ms()}],
            )

    def _import_records_sync(self, records: list[dict[str, Any]]) -> None:
        with self._sync_engine.begin() as conn:
            conn.execute(insert(call_log_entries), records)

    def _render_csv_chunk(self, rows: list[list[Any]], with_bom: bool = False) -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerows(rows)
        text = buffer.getvalue()
        if with_bom:
            text = "\ufeff" + text
        return text.encode("utf-8")


class CallLogStoreFactory:
    _instance: CallLogStore | None = None

    @classmethod
    def get_store(cls) -> CallLogStore:
        if cls._instance is None:
            target = _resolve_store_target()
            logger.info(
                "Call log store initialized",
                extra={
                    "backend": target["backend"],
                    "operation": "call_log_store_init",
                },
            )
            cls._instance = CallLogStore(target)
        return cls._instance

    @classmethod
    async def close(cls) -> None:
        if cls._instance is None:
            return
        await cls._instance.close()
        cls._instance = None


def get_call_log_store() -> CallLogStore:
    return CallLogStoreFactory.get_store()
