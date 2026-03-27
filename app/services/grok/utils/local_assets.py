"""
Local asset storage helpers.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import get_config
from app.core.storage import DATA_DIR

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}


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

    async def write_bytes(self, media_type: str, filename: str, payload: bytes) -> Path:
        safe_name = self.sanitize_filename(filename or "", fallback=f"{media_type}-asset")
        target_path = self.cache_dir(media_type) / safe_name
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")

        def _write_file():
            with open(tmp_path, "wb") as file:
                file.write(payload)
            tmp_path.replace(target_path)

        await asyncio.to_thread(_write_file)
        return target_path


__all__ = ["IMAGE_EXTS", "VIDEO_EXTS", "LocalAssetStore"]
