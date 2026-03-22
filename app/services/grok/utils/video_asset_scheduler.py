"""
Periodic cleanup for persistent local videos.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.logger import logger
from app.services.grok.utils.video_assets import VideoAssetService


class VideoAssetCleanupScheduler:
    def __init__(self, interval_seconds: int = 3600):
        self.interval_seconds = max(300, int(interval_seconds or 3600))
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _loop(self):
        while self._running:
            try:
                removed = await VideoAssetService.cleanup_expired_files()
                if removed > 0:
                    logger.info(f"VideoAssetCleanup: removed {removed} expired files")
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"VideoAssetCleanup: {exc}")
                await asyncio.sleep(min(self.interval_seconds, 300))

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("VideoAssetCleanup: scheduler enabled")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()


_scheduler: Optional[VideoAssetCleanupScheduler] = None


def get_video_asset_scheduler() -> VideoAssetCleanupScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = VideoAssetCleanupScheduler()
    return _scheduler


__all__ = ["VideoAssetCleanupScheduler", "get_video_asset_scheduler"]
