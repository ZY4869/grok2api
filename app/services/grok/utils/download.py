"""
Download service.

Download service for assets.grok.com.
"""

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from app.core.logger import logger
from app.core.config import get_config
from app.core.exceptions import AppException
from app.services.grok.utils.local_assets import LocalAssetStore
from app.services.reverse.assets_download import AssetsDownloadReverse
from app.services.reverse.utils.session import ResettableSession
from app.services.grok.utils.locks import _get_download_semaphore, _file_lock


class DownloadService:
    """Assets download service."""

    def __init__(self):
        self._session: Optional[ResettableSession] = None
        self.store = LocalAssetStore()
        self._cleanup_running = False

    async def create(self) -> ResettableSession:
        """Create or reuse a session."""
        if self._session is None:
            browser = get_config("proxy.browser")
            if browser:
                self._session = ResettableSession(impersonate=browser)
            else:
                self._session = ResettableSession()
        return self._session

    async def close(self):
        """Close the session."""
        if self._session:
            await self._session.close()
            self._session = None

    def _asset_url(self, file_path: str) -> str:
        """Return the public asset URL for a relative path or validated absolute URL."""
        if not isinstance(file_path, str) or not file_path.strip():
            raise AppException("Invalid file path", code="invalid_file_path")

        value = file_path.strip()
        if value.startswith("data:"):
            raise AppException("Invalid file path", code="invalid_file_path")

        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc:
            if not (
                parsed.scheme and parsed.netloc and parsed.scheme in ["http", "https"]
            ):
                raise AppException("Invalid file path", code="invalid_file_path")
            return value

        if not value.startswith("/"):
            value = f"/{value}"
        return f"https://assets.grok.com{value}"

    def _normalize_download_target(
        self, file_path: str, preserve_absolute: bool = False
    ) -> str:
        """Normalize URL or path for download while optionally keeping absolute URLs."""
        asset_url = self._asset_url(file_path)
        if preserve_absolute:
            return asset_url

        parsed = urlparse(asset_url)
        path = parsed.path or ""
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if not path:
            raise AppException("Invalid file path", code="invalid_file_path")
        if not path.startswith("/"):
            path = f"/{path}"
        return path

    async def _download_to_cache(
        self,
        request_target: str,
        token: str,
        media_type: str,
        filename: str,
    ) -> Tuple[Optional[Path], str]:
        async with _get_download_semaphore():
            cache_dir = self.store.cache_dir(media_type)
            safe_name = self.store.sanitize_filename(
                filename or "", fallback=f"{media_type}-asset"
            )
            cache_path = cache_dir / safe_name

            lock_name = (
                f"dl_{media_type}_{hashlib.sha1(str(cache_path).encode()).hexdigest()[:16]}"
            )
            lock_timeout = max(1, int(get_config("asset.download_timeout")))
            async with _file_lock(lock_name, timeout=lock_timeout):
                session = await self.create()
                response = await AssetsDownloadReverse.request(
                    session, token, request_target
                )

                if hasattr(response, "aiter_content"):
                    chunks = bytearray()
                    async for chunk in response.aiter_content():
                        if chunk:
                            chunks.extend(chunk)
                    payload = bytes(chunks)
                else:
                    payload = response.content

                cache_path = await self.store.write_bytes(
                    media_type,
                    safe_name,
                    payload,
                )

                mime = response.headers.get(
                    "content-type", "application/octet-stream"
                ).split(";")[0]
                logger.info(f"Downloaded: {request_target}")
                asyncio.create_task(self._check_limit())

            return cache_path, mime

    async def localize_video_asset(
        self, path_or_url: str, token: str, media_type: str = "video", asset_type: str = ""
    ) -> str:
        """Download a video result asset first and return a local files URL."""
        asset_url = path_or_url
        try:
            asset_url = self._asset_url(path_or_url)
            request_target = self._normalize_download_target(
                path_or_url, preserve_absolute=True
            )
            filename = self.store.build_source_filename(path_or_url, media_type)
            cache_path, _ = await self._download_to_cache(
                request_target, token, media_type, filename
            )
            local_url = self.store.build_public_url(media_type, cache_path.name)
            logger.info(
                "Localized video asset",
                extra={
                    "asset_type": asset_type or media_type,
                    "upstream_host": urlparse(asset_url).netloc or "assets.grok.com",
                    "download_result": "localized",
                    "cache_name": cache_path.name,
                },
            )
            return local_url
        except Exception as e:
            logger.warning(
                "Localized video asset fallback",
                extra={
                    "asset_type": asset_type or media_type,
                    "upstream_host": urlparse(asset_url).netloc or "assets.grok.com",
                    "download_result": "fallback",
                    "fallback_reason": str(e),
                    "cache_name": "",
                },
            )
            return asset_url

    async def resolve_url(
        self, path_or_url: str, token: str, media_type: str = "image"
    ) -> str:
        asset_url = self._asset_url(path_or_url)
        try:
            filename = self.store.build_source_filename(asset_url, media_type)
            cache_path, _ = await self._download_to_cache(
                asset_url,
                token,
                media_type,
                filename,
            )
            local_url = self.store.build_public_url(media_type, cache_path.name)
            logger.info(
                "Localized image asset",
                extra={
                    "asset_type": media_type,
                    "upstream_host": urlparse(asset_url).netloc or "assets.grok.com",
                    "download_result": "localized",
                    "cache_name": cache_path.name,
                },
            )
            return local_url
        except Exception as e:
            logger.warning(
                "Localized image asset fallback",
                extra={
                    "asset_type": media_type,
                    "upstream_host": urlparse(asset_url).netloc or "assets.grok.com",
                    "download_result": "fallback",
                    "fallback_reason": str(e),
                    "cache_name": "",
                },
            )
            return asset_url

    async def render_image(
        self, url: str, token: str, image_id: str = "image"
    ) -> str:
        fmt = get_config("app.image_format")
        fmt = fmt.lower() if isinstance(fmt, str) else "url"
        if fmt not in ("base64", "url", "markdown"):
            fmt = "url"
        try:
            if fmt == "base64":
                data_uri = await self.parse_b64(url, token, "image")
                return f"![{image_id}]({data_uri})"
            final_url = await self.resolve_url(url, token, "image")
            return f"![{image_id}]({final_url})"
        except Exception as e:
            logger.warning(f"Image render failed, fallback to URL: {e}")
            final_url = await self.resolve_url(url, token, "image")
            return f"![{image_id}]({final_url})"

    async def render_video(
        self, video_url: str, token: str, thumbnail_url: str = ""
    ) -> str:
        fmt = get_config("app.video_format")
        fmt = fmt.lower() if isinstance(fmt, str) else "url"
        if fmt not in ("url", "markdown", "html"):
            fmt = "url"
        final_video_url = await self.localize_video_asset(
            video_url, token, "video", asset_type="video"
        )
        final_thumb_url = ""
        if thumbnail_url:
            final_thumb_url = await self.localize_video_asset(
                thumbnail_url, token, "image", asset_type="thumbnail"
            )
        if fmt == "url":
            return f"{final_video_url}\n"
        if fmt == "markdown":
            return f"[video]({final_video_url})"
        import html

        safe_video_url = html.escape(final_video_url)
        safe_thumbnail_url = html.escape(final_thumb_url)
        poster_attr = f' poster="{safe_thumbnail_url}"' if safe_thumbnail_url else ""
        return f'''<video id="video" controls="" preload="none"{poster_attr}>
  <source id="mp4" src="{safe_video_url}" type="video/mp4">
</video>'''

    async def parse_b64(self, file_path: str, token: str, media_type: str = "image") -> str:
        """Download and return data URI."""
        try:
            if not isinstance(file_path, str) or not file_path.strip():
                raise AppException("Invalid file path", code="invalid_file_path")
            if file_path.startswith("data:"):
                raise AppException("Invalid file path", code="invalid_file_path")
            file_path = self._asset_url(file_path)
            lock_name = f"dl_b64_{hashlib.sha1(file_path.encode()).hexdigest()[:16]}"
            lock_timeout = max(1, int(get_config("asset.download_timeout")))
            async with _get_download_semaphore():
                async with _file_lock(lock_name, timeout=lock_timeout):
                    session = await self.create()
                    response = await AssetsDownloadReverse.request(
                        session, token, file_path
                    )

            if hasattr(response, "aiter_content"):
                data = bytearray()
                async for chunk in response.aiter_content():
                    if chunk:
                        data.extend(chunk)
                raw = bytes(data)
            else:
                raw = response.content

            content_type = response.headers.get(
                "content-type", "application/octet-stream"
            ).split(";")[0]
            data_uri = f"data:{content_type};base64,{base64.b64encode(raw).decode()}"

            return data_uri
        except Exception as e:
            logger.error(f"Failed to convert {file_path} to base64: {e}")
            raise

    def _normalize_path(self, file_path: str) -> str:
        """Normalize URL or path to assets path for download."""
        return self._normalize_download_target(file_path, preserve_absolute=False)

    async def download_file(self, file_path: str, token: str, media_type: str = "image") -> Tuple[Optional[Path], str]:
        """Download asset to local cache.

        Args:
            file_path: str, the path of the file to download.
            token: str, the SSO token.
            media_type: str, the media type of the file.

        Returns:
            Tuple[Optional[Path], str]: The path of the downloaded file and the MIME type.
        """
        request_target = self._normalize_download_target(file_path, preserve_absolute=True)
        filename = self.store.build_source_filename(file_path, media_type)
        return await self._download_to_cache(
            request_target, token, media_type, filename
        )

    async def _check_limit(self):
        """Check cache limit and cleanup.

        Args:
            self: DownloadService, the download service instance.

        Returns:
            None
        """
        if self._cleanup_running or not get_config("cache.enable_auto_clean"):
            return

        self._cleanup_running = True
        try:
            try:
                async with _file_lock("cache_cleanup", timeout=5):
                    limit_mb = get_config("cache.limit_mb")
                    total_size = 0
                    all_files: List[Tuple[Path, float, int]] = []

                    for d in [self.store.image_dir, self.store.video_dir]:
                        if d.exists():
                            for f in d.glob("*"):
                                if f.is_file():
                                    try:
                                        stat = f.stat()
                                        total_size += stat.st_size
                                        all_files.append(
                                            (f, stat.st_mtime, stat.st_size)
                                        )
                                    except Exception:
                                        pass
                    current_mb = total_size / 1024 / 1024

                    if current_mb <= limit_mb:
                        return

                    logger.info(
                        f"Cache limit exceeded ({current_mb:.2f}MB > {limit_mb}MB), cleaning..."
                    )
                    all_files.sort(key=lambda x: x[1])

                    deleted_count = 0
                    deleted_size = 0
                    target_mb = limit_mb * 0.8

                    for f, _, size in all_files:
                        try:
                            f.unlink()
                            deleted_count += 1
                            deleted_size += size
                            total_size -= size
                            if (total_size / 1024 / 1024) <= target_mb:
                                break
                        except Exception:
                            pass

                    logger.info(
                        f"Cache cleanup: {deleted_count} files ({deleted_size / 1024 / 1024:.2f}MB)"
                    )
            except Exception as e:
                logger.warning(f"Cache cleanup failed: {e}")
        finally:
            self._cleanup_running = False


__all__ = ["DownloadService"]
