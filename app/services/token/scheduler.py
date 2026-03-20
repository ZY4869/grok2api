"""Token 刷新调度器"""

import asyncio
from typing import Optional

from app.core.logger import logger
from app.core.storage import get_storage, StorageError, RedisStorage
from app.services.token.manager import get_token_manager


class TokenRefreshScheduler:
    """Token 自动刷新调度器"""

    def __init__(self, interval_hours: int = 8):
        self.interval_hours = interval_hours
        self.interval_seconds = interval_hours * 3600
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _refresh_loop(self):
        """刷新循环"""
        logger.info(f"Scheduler: started (interval: {self.interval_hours}h)")

        while self._running:
            try:
                storage = get_storage()
                lock_acquired = False
                lock = None

                if isinstance(storage, RedisStorage):
                    # Redis: non-blocking lock to avoid multi-worker duplication
                    lock_key = "grok2api:lock:token_refresh"
                    lock = storage.redis.lock(
                        lock_key, timeout=self.interval_seconds + 60, blocking_timeout=0
                    )
                    lock_acquired = await lock.acquire(blocking=False)
                else:
                    try:
                        async with storage.acquire_lock("token_refresh", timeout=1):
                            lock_acquired = True
                    except StorageError:
                        lock_acquired = False

                if not lock_acquired:
                    logger.info("Scheduler: skipped (lock not acquired)")
                    await asyncio.sleep(self.interval_seconds)
                    continue

                try:
                    logger.info("Scheduler: starting token refresh...")
                    manager = await get_token_manager()
                    result = await manager.refresh_cooling_tokens()

                    logger.info(
                        f"Scheduler: refresh completed - "
                        f"checked={result['checked']}, "
                        f"refreshed={result['refreshed']}, "
                        f"recovered={result['recovered']}, "
                        f"expired={result['expired']}"
                    )
                finally:
                    if lock is not None and lock_acquired:
                        try:
                            await lock.release()
                        except Exception:
                            pass

                await asyncio.sleep(self.interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler: refresh error - {e}")
                await asyncio.sleep(self.interval_seconds)

    def start(self):
        """启动调度器"""
        if self._running:
            logger.warning("Scheduler: already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._refresh_loop())
        logger.info("Scheduler: enabled")

    def stop(self):
        """停止调度器"""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("Scheduler: stopped")


class AccountCheckScheduler:
    """账号可用性定时检测调度器"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _check_loop(self):
        from app.core.config import get_config

        while self._running:
            try:
                enabled = get_config("account.auto_check_enabled", False)
                interval_hours = get_config("account.auto_check_interval_hours", 2)
                auto_clean = get_config("account.auto_clean_expired", False)
                interval_seconds = max(int(interval_hours) * 3600, 600)

                if not enabled:
                    await asyncio.sleep(60)
                    continue

                logger.info("AccountCheck: starting alive check for all tokens...")
                manager = await get_token_manager()

                alive_count = 0
                expired_count = 0
                for pool in manager.pools.values():
                    for token_info in pool.list():
                        result = await manager.check_alive(token_info.token)
                        if result is True:
                            alive_count += 1
                        elif result is False:
                            expired_count += 1

                logger.info(f"AccountCheck: completed - alive={alive_count}, expired={expired_count}")

                if auto_clean and expired_count > 0:
                    removed = 0
                    for pool in manager.pools.values():
                        to_remove = [t.token for t in pool.list() if t.alive is False or t.status.value == "expired"]
                        for token_str in to_remove:
                            await manager.remove(token_str)
                            removed += 1
                    if removed > 0:
                        await manager._save(force=True)
                        logger.info(f"AccountCheck: auto-cleaned {removed} expired tokens")

                await asyncio.sleep(interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AccountCheck: error - {e}")
                await asyncio.sleep(300)

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("AccountCheck: scheduler enabled")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()


# 全局单例
_scheduler: Optional[TokenRefreshScheduler] = None
_account_scheduler: Optional[AccountCheckScheduler] = None


def get_scheduler(interval_hours: int = 8) -> TokenRefreshScheduler:
    """获取调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TokenRefreshScheduler(interval_hours)
    return _scheduler


def get_account_scheduler() -> AccountCheckScheduler:
    """获取账号检测调度器单例"""
    global _account_scheduler
    if _account_scheduler is None:
        _account_scheduler = AccountCheckScheduler()
    return _account_scheduler


__all__ = ["TokenRefreshScheduler", "get_scheduler", "AccountCheckScheduler", "get_account_scheduler"]
