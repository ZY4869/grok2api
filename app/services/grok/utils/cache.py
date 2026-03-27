"""
Local cache utilities.
"""

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
            view_url = self.store.build_public_url(media_type, item["name"])
            item["view_url"] = view_url
            if media_type == "image":
                item["preview_url"] = view_url

        return {"total": total, "page": page, "page_size": page_size, "items": paged}

    def delete_file(self, media_type: str, name: str) -> Dict[str, Any]:
        cache_dir = self._cache_dir(media_type)
        file_path = cache_dir / name.replace("/", "-")

        if file_path.exists():
            try:
                file_path.unlink()
                return {"deleted": True}
            except Exception:
                pass
        return {"deleted": False}

    def clear(self, media_type: str = "image") -> Dict[str, Any]:
        cache_dir = self._cache_dir(media_type)
        if not cache_dir.exists():
            return {"count": 0, "size_mb": 0.0}

        files = list(cache_dir.glob("*"))
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        count = 0

        for f in files:
            if f.is_file():
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass

        return {"count": count, "size_mb": round(total_size / 1024 / 1024, 2)}


__all__ = ["CacheService"]
