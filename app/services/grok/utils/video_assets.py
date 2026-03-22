"""
Persistent local video asset helpers.
"""

from __future__ import annotations

import os
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiofiles

from app.core.config import get_config
from app.core.exceptions import UpstreamException, ValidationException
from app.core.logger import logger
from app.core.storage import DATA_DIR
from app.services.reverse.assets_download import AssetsDownloadReverse
from app.services.reverse.utils.session import ResettableSession

VIDEO_MEDIA_DIR = DATA_DIR / "media" / "video"
LEGACY_VIDEO_DIR = DATA_DIR / "tmp" / "video"
_LOCAL_VIDEO_PREFIX = "/v1/files/video/"
_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
_EXT_BY_MIME = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


@dataclass
class VideoDeliveryMeta:
    url: str
    storage: str = ""
    expires_at: Optional[int] = None
    path: str = ""
    media_type: str = "video/mp4"

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "storage": self.storage,
            "expires_at": self.expires_at,
            "path": self.path,
            "media_type": self.media_type,
        }


class VideoAssetService:
    """Persist generated videos under DATA_DIR/media/video."""

    @staticmethod
    def local_persist_enabled() -> bool:
        return bool(get_config("video.local_persist_enabled", False))

    @staticmethod
    def retention_days() -> int:
        try:
            return max(0, int(get_config("video.local_retention_days", 7) or 0))
        except Exception:
            return 7

    @classmethod
    def media_dir(cls) -> Path:
        VIDEO_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        return VIDEO_MEDIA_DIR

    @staticmethod
    def build_local_url(filename: str) -> str:
        app_url = str(get_config("app.app_url") or "").strip()
        path = f"{_LOCAL_VIDEO_PREFIX}{filename}"
        if app_url:
            return f"{app_url.rstrip('/')}{path}"
        return path

    @classmethod
    def is_local_video_url(cls, value: str) -> bool:
        candidate = (value or "").strip()
        if not candidate:
            return False
        if candidate.startswith(_LOCAL_VIDEO_PREFIX):
            return True
        app_url = str(get_config("app.app_url") or "").strip().rstrip("/")
        if not app_url:
            return False
        return candidate.startswith(f"{app_url}{_LOCAL_VIDEO_PREFIX}")

    @classmethod
    def extract_filename(cls, value: str) -> str:
        candidate = (value or "").strip()
        if not candidate:
            return ""
        if candidate.startswith(_LOCAL_VIDEO_PREFIX):
            return candidate[len(_LOCAL_VIDEO_PREFIX) :].strip()
        parsed = urlparse(candidate)
        if not parsed.path.startswith(_LOCAL_VIDEO_PREFIX):
            return ""
        return parsed.path[len(_LOCAL_VIDEO_PREFIX) :].strip()

    @classmethod
    def resolve_file_path(cls, filename: str) -> Optional[Path]:
        safe_name = filename.replace("/", "-").replace("\\", "-")
        for base_dir in (cls.media_dir(), LEGACY_VIDEO_DIR):
            path = base_dir / safe_name
            if path.exists() and path.is_file():
                return path
        return None

    @classmethod
    def media_type_for_path(cls, path: Path) -> str:
        return _MIME_BY_EXT.get(path.suffix.lower(), "video/mp4")

    @classmethod
    def expires_at_for_path(cls, path: Path) -> Optional[int]:
        retention_days = cls.retention_days()
        if retention_days <= 0:
            return None
        try:
            return int(path.stat().st_mtime) + (retention_days * 24 * 3600)
        except Exception:
            return None

    @classmethod
    def _filename_for_source(cls, source_url: str, mime_type: str) -> str:
        parsed = urlparse(source_url)
        raw_name = Path(parsed.path or "").name
        ext = Path(raw_name).suffix.lower()
        if ext not in _MIME_BY_EXT:
            ext = _EXT_BY_MIME.get(mime_type, ".mp4")
        stem = Path(raw_name).stem or "video"
        stem = "".join(ch for ch in stem if ch.isalnum() or ch in ("-", "_")) or "video"
        digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16]
        return f"{stem}-{digest}{ext}"

    @classmethod
    async def persist_video(cls, source_url: str, token: str) -> VideoDeliveryMeta:
        existing_name = cls.extract_filename(source_url)
        if existing_name:
            existing_path = cls.resolve_file_path(existing_name)
            if existing_path:
                return VideoDeliveryMeta(
                    url=cls.build_local_url(existing_path.name),
                    storage="local",
                    expires_at=cls.expires_at_for_path(existing_path),
                    path=str(existing_path),
                    media_type=cls.media_type_for_path(existing_path),
                )

        async with ResettableSession() as session:
            response = await AssetsDownloadReverse.request(session, token, source_url)
            mime_type = (
                str(response.headers.get("content-type") or "video/mp4").split(";", 1)[0].strip().lower()
            )
            filename = cls._filename_for_source(source_url, mime_type)
            target_path = cls.media_dir() / filename
            temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
            try:
                async with aiofiles.open(temp_path, "wb") as file_obj:
                    if hasattr(response, "aiter_content"):
                        async for chunk in response.aiter_content():
                            if chunk:
                                await file_obj.write(chunk)
                    else:
                        await file_obj.write(response.content)
                os.replace(temp_path, target_path)
            finally:
                if temp_path.exists() and not target_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass

        logger.info(f"Persistent video saved: {target_path.name}")
        return VideoDeliveryMeta(
            url=cls.build_local_url(target_path.name),
            storage="local",
            expires_at=cls.expires_at_for_path(target_path),
            path=str(target_path),
            media_type=cls.media_type_for_path(target_path),
        )

    @classmethod
    async def prepare_delivery(
        cls,
        source_url: str,
        token: str,
        delivery_mode: str,
    ) -> VideoDeliveryMeta:
        mode = str(delivery_mode or "url").strip().lower() or "url"
        if not cls.local_persist_enabled():
            if mode == "file":
                raise ValidationException(
                    message="delivery_mode=file requires video.local_persist_enabled=true",
                    param="delivery_mode",
                    code="invalid_delivery_mode",
                )
            return VideoDeliveryMeta(url=source_url)
        try:
            return await cls.persist_video(source_url, token)
        except Exception as exc:
            if mode == "file":
                logger.warning(f"Persistent video file delivery failed: {exc}")
                raise UpstreamException(
                    message="Video local persistence failed",
                    status_code=502,
                    code="video_local_persist_failed",
                ) from exc
            logger.warning(f"Persistent video fallback to upstream URL: {exc}")
            return VideoDeliveryMeta(url=source_url, storage="upstream_fallback")

    @classmethod
    async def cleanup_expired_files(cls) -> int:
        retention_days = cls.retention_days()
        if retention_days <= 0:
            return 0
        cutoff = time.time() - (retention_days * 24 * 3600)
        removed = 0
        media_dir = cls.media_dir()
        for path in media_dir.glob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
            except Exception as exc:
                logger.warning(f"Video cleanup skipped {path.name}: {exc}")
        return removed


__all__ = [
    "LEGACY_VIDEO_DIR",
    "VIDEO_MEDIA_DIR",
    "VideoAssetService",
    "VideoDeliveryMeta",
]
