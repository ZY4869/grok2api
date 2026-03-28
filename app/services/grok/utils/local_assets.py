"""
Local asset storage helpers.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import urlparse

import orjson

from app.core.config import get_config
from app.core.call_log import get_call_log_context
from app.core.storage import DATA_DIR

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
METADATA_SUFFIX = ".meta.json"


class LocalAssetStore:
    """Shared local storage for generated and downloaded assets."""

    def __init__(self):
        self.base_dir = DATA_DIR / "tmp"
        self.image_dir = self.base_dir / "image"
        self.video_dir = self.base_dir / "video"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.video_dir.mkdir(parents=True, exist_ok=True)

    def cache_dir(self, media_type: str) -> Path:
        return self.video_dir if media_type == "video" else self.image_dir

    def allowed_exts(self, media_type: str) -> set[str]:
        return VIDEO_EXTS if media_type == "video" else IMAGE_EXTS

    def sanitize_filename(self, value: str, fallback: str = "asset") -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
        return cleaned or fallback

    def normalize_ext(self, ext: str | None, media_type: str = "image") -> str:
        normalized = str(ext or "").strip().lower().lstrip(".")
        if normalized == "jpeg":
            normalized = "jpg"
        allowed = {item.lstrip(".") for item in self.allowed_exts(media_type)}
        if normalized in allowed:
            return normalized
        return "mp4" if media_type == "video" else "jpg"

    def build_public_url(self, media_type: str, filename: str) -> str:
        safe_name = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        path = f"/v1/files/{media_type}/{safe_name}"
        app_url = get_config("app.app_url") or ""
        if app_url:
            return f"{app_url.rstrip('/')}{path}"
        return path

    def build_generated_image_filename(
        self, image_id: str, *, is_final: bool, ext: str | None = None
    ) -> str:
        safe_id = self.sanitize_filename(image_id or "", fallback="image")
        normalized_ext = self.normalize_ext(ext, "image")
        if not ext:
            normalized_ext = "jpg" if is_final else "png"
        return f"{safe_id}.{normalized_ext}"

    def build_source_filename(
        self, source_url: str, media_type: str = "image", ext: str | None = None
    ) -> str:
        parsed = urlparse(str(source_url or "").strip())
        path = parsed.path or ""
        suffix = Path(path).suffix.lower().lstrip(".")
        final_ext = self.normalize_ext(ext or suffix, media_type)
        stem_path = path[: -len(Path(path).suffix)] if Path(path).suffix else path
        stem = self.sanitize_filename((stem_path.strip("/") or "root").replace("/", "-"))
        host = self.sanitize_filename(parsed.netloc.lower() or "asset-host")
        digest = hashlib.sha1(str(source_url or "").encode("utf-8")).hexdigest()[:12]
        return f"{host}-{stem}-{digest}.{final_ext}"

    def metadata_path(self, media_type: str, filename: str) -> Path:
        safe_name = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        return self.cache_dir(media_type) / f"{safe_name}{METADATA_SUFFIX}"

    def _mask_token(self, token: str) -> str:
        value = str(token or "").replace("sso=", "").strip()
        if not value:
            return ""
        if len(value) <= 24:
            return value
        return f"{value[:8]}...{value[-16:]}"

    def _default_metadata(
        self, media_type: str, filename: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        context = get_call_log_context()
        token = (
            str((metadata or {}).get("token") or "")
            or str(getattr(context, "token", "") or "")
        ).replace("sso=", "").strip()
        pool = str((metadata or {}).get("pool") or getattr(context, "pool", "") or "").strip()
        email = str((metadata or {}).get("email") or getattr(context, "email", "") or "").strip()
        trace_id = str((metadata or {}).get("trace_id") or getattr(context, "trace_id", "") or "").strip()
        model = str((metadata or {}).get("model") or getattr(context, "model", "") or "").strip()
        created_at = int(
            (metadata or {}).get("created_at")
            or getattr(context, "started_at", 0)
            or int(time() * 1000)
        )
        creator_accounts: list[dict[str, str]] = []
        if token or email or pool:
            creator_accounts.append(
                {
                    "token": token,
                    "token_masked": self._mask_token(token),
                    "email": email,
                    "pool": pool,
                }
            )
        return {
            "media_type": media_type,
            "name": self.sanitize_filename(filename or "", fallback=f"{media_type}-asset"),
            "source_url": str((metadata or {}).get("source_url") or "").strip(),
            "origin_kind": str((metadata or {}).get("origin_kind") or "").strip(),
            "model": model,
            "created_at": created_at,
            "updated_at": int(time() * 1000),
            "trace_ids": [trace_id] if trace_id else [],
            "creator_accounts": creator_accounts,
        }

    def _read_metadata(self, media_type: str, filename: str) -> dict[str, Any]:
        path = self.metadata_path(media_type, filename)
        if not path.exists():
            return {}
        try:
            payload = orjson.loads(path.read_bytes())
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _merge_metadata(
        self,
        media_type: str,
        filename: str,
        current: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(current or {})
        payload["media_type"] = media_type
        payload["name"] = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        for field in ("source_url", "origin_kind", "model"):
            if incoming.get(field):
                payload[field] = incoming[field]
            else:
                payload.setdefault(field, str(payload.get(field) or "").strip())

        current_created_at = int(payload.get("created_at") or 0)
        incoming_created_at = int(incoming.get("created_at") or 0)
        candidates = [value for value in (current_created_at, incoming_created_at) if value > 0]
        payload["created_at"] = min(candidates) if candidates else int(time() * 1000)
        payload["updated_at"] = int(time() * 1000)

        trace_ids = []
        for value in list(payload.get("trace_ids") or []) + list(incoming.get("trace_ids") or []):
            trace_id = str(value or "").strip()
            if trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)
        payload["trace_ids"] = trace_ids

        merged_accounts: list[dict[str, str]] = []
        seen_accounts: set[str] = set()
        for account in list(payload.get("creator_accounts") or []) + list(
            incoming.get("creator_accounts") or []
        ):
            if not isinstance(account, dict):
                continue
            token = str(account.get("token") or "").replace("sso=", "").strip()
            email = str(account.get("email") or "").strip()
            pool = str(account.get("pool") or "").strip()
            if not token and not email and not pool:
                continue
            key = f"{token}|{pool}|{email}"
            if key in seen_accounts:
                continue
            seen_accounts.add(key)
            merged_accounts.append(
                {
                    "token": token,
                    "token_masked": self._mask_token(token),
                    "email": email,
                    "pool": pool,
                }
            )
        payload["creator_accounts"] = merged_accounts
        return payload

    async def update_metadata(
        self, media_type: str, filename: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        safe_name = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        base_metadata = self._default_metadata(media_type, safe_name, metadata)
        meta_path = self.metadata_path(media_type, safe_name)

        def _write_metadata() -> dict[str, Any]:
            current = self._read_metadata(media_type, safe_name)
            merged = self._merge_metadata(media_type, safe_name, current, base_metadata)
            tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
            tmp_path.write_bytes(orjson.dumps(merged))
            tmp_path.replace(meta_path)
            return merged

        return await asyncio.to_thread(_write_metadata)

    def get_metadata(self, media_type: str, filename: str) -> dict[str, Any]:
        safe_name = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        return self._read_metadata(media_type, safe_name)

    def delete_asset_sync(self, media_type: str, filename: str) -> bool:
        safe_name = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        target_path = self.cache_dir(media_type) / safe_name
        meta_path = self.metadata_path(media_type, safe_name)
        deleted = False
        if target_path.exists():
            target_path.unlink()
            deleted = True
        if meta_path.exists():
            meta_path.unlink()
        return deleted

    async def delete_asset(self, media_type: str, filename: str) -> bool:
        safe_name = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        return await asyncio.to_thread(self.delete_asset_sync, media_type, safe_name)

    async def write_bytes(
        self,
        media_type: str,
        filename: str,
        payload: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        safe_name = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        target_path = self.cache_dir(media_type) / safe_name
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")

        def _write_file():
            with open(tmp_path, "wb") as file:
                file.write(payload)
            tmp_path.replace(target_path)

        await asyncio.to_thread(_write_file)
        await self.update_metadata(media_type, safe_name, metadata)
        return target_path


__all__ = ["IMAGE_EXTS", "VIDEO_EXTS", "METADATA_SUFFIX", "LocalAssetStore"]
