"""
Local cache utilities.
"""

import csv
import io
from typing import Any, Dict

from app.services.grok.utils.local_assets import LocalAssetStore


class CacheService:
    """Local cache service."""

    def __init__(self):
        self.store = LocalAssetStore()

    def _cache_dir(self, media_type: str):
        return self.store.cache_dir(media_type)

    def _allowed_exts(self, media_type: str):
        return self.store.allowed_exts(media_type)

    def _creator_label(self, account: dict[str, Any]) -> str:
        email = str(account.get("email") or "").strip()
        token_masked = str(account.get("token_masked") or "").strip()
        token = str(account.get("token") or "").strip()
        pool = str(account.get("pool") or "").strip()
        base = email or token_masked or token or "未知账号"
        return f"{base} ({pool})" if pool else base

    def _decorate_item(self, media_type: str, item: dict[str, Any]) -> dict[str, Any]:
        metadata = self.store.get_metadata(media_type, item["name"])
        creator_accounts = list(metadata.get("creator_accounts") or [])
        creator_labels = [self._creator_label(account) for account in creator_accounts]
        creator_display = creator_labels[0] if creator_labels else "未知账号"
        if len(creator_labels) > 1:
            creator_display = f"{creator_display} +{len(creator_labels) - 1}"

        view_url = self.store.build_public_url(media_type, item["name"])
        item["view_url"] = view_url
        item["creator_accounts"] = creator_accounts
        item["creator_count"] = len(creator_accounts)
        item["creator_display"] = creator_display
        item["creator_details"] = creator_labels
        item["source_kind"] = str(metadata.get("origin_kind") or "").strip()
        item["source_url"] = str(metadata.get("source_url") or "").strip()
        item["trace_ids"] = list(metadata.get("trace_ids") or [])
        if media_type == "image":
            item["preview_url"] = view_url
        return item

    def get_stats(self, media_type: str = "image") -> Dict[str, Any]:
        cache_dir = self._cache_dir(media_type)
        if not cache_dir.exists():
            return {"count": 0, "size_mb": 0.0}

        allowed = self._allowed_exts(media_type)
        files = [
            f for f in cache_dir.glob("*") if f.is_file() and f.suffix.lower() in allowed
        ]
        total_size = sum(f.stat().st_size for f in files)
        return {"count": len(files), "size_mb": round(total_size / 1024 / 1024, 2)}

    def list_files(
        self, media_type: str = "image", page: int = 1, page_size: int = 1000
    ) -> Dict[str, Any]:
        cache_dir = self._cache_dir(media_type)
        if not cache_dir.exists():
            return {"total": 0, "page": page, "page_size": page_size, "items": []}

        allowed = self._allowed_exts(media_type)
        files = [
            f for f in cache_dir.glob("*") if f.is_file() and f.suffix.lower() in allowed
        ]

        items = []
        for f in files:
            try:
                stat = f.stat()
                items.append(
                    {
                        "name": f.name,
                        "size_bytes": stat.st_size,
                        "mtime_ms": int(stat.st_mtime * 1000),
                    }
                )
            except Exception:
                continue

        items.sort(key=lambda x: x["mtime_ms"], reverse=True)

        total = len(items)
        start = max(0, (page - 1) * page_size)
        paged = items[start : start + page_size]

        for item in paged:
            self._decorate_item(media_type, item)

        return {"total": total, "page": page, "page_size": page_size, "items": paged}

    def delete_file(self, media_type: str, name: str) -> Dict[str, Any]:
        try:
            deleted = self.store.delete_asset_sync(media_type, name.replace("/", "-"))
            return {"deleted": deleted}
        except Exception:
            return {"deleted": False}

    def clear(self, media_type: str = "image") -> Dict[str, Any]:
        cache_dir = self._cache_dir(media_type)
        if not cache_dir.exists():
            return {"count": 0, "size_mb": 0.0}

        allowed = self._allowed_exts(media_type)
        files = [f for f in cache_dir.glob("*") if f.is_file() and f.suffix.lower() in allowed]
        total_size = sum(f.stat().st_size for f in files)
        count = 0

        for f in files:
            try:
                self.delete_file(media_type, f.name)
                count += 1
            except Exception:
                pass

        return {"count": count, "size_mb": round(total_size / 1024 / 1024, 2)}

    def export_csv(self, media_type: str = "image") -> bytes:
        result = self.list_files(media_type, page=1, page_size=1000000)
        headers = [
            "name",
            "size_bytes",
            "mtime_ms",
            "view_url",
            "source_kind",
            "source_url",
            "creator_emails",
            "creator_tokens",
            "creator_pools",
            "trace_ids",
        ]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers)
        for item in result["items"]:
            creators = list(item.get("creator_accounts") or [])
            writer.writerow(
                [
                    item.get("name", ""),
                    item.get("size_bytes", 0),
                    item.get("mtime_ms", 0),
                    item.get("view_url", ""),
                    item.get("source_kind", ""),
                    item.get("source_url", ""),
                    " | ".join(
                        str(account.get("email") or "").strip()
                        for account in creators
                        if str(account.get("email") or "").strip()
                    ),
                    " | ".join(
                        str(account.get("token_masked") or account.get("token") or "").strip()
                        for account in creators
                        if str(account.get("token") or "").strip()
                    ),
                    " | ".join(
                        str(account.get("pool") or "").strip()
                        for account in creators
                        if str(account.get("pool") or "").strip()
                    ),
                    " | ".join(str(value or "").strip() for value in item.get("trace_ids") or []),
                ]
            )
        return ("\ufeff" + buffer.getvalue()).encode("utf-8")


__all__ = ["CacheService"]
