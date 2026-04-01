"""
Token 数据模型

额度规则:
- Basic 新号默认 80 配额
- Super 新号默认 140 配额
- 重置后恢复默认值
- lowEffort 扣 1，highEffort 扣 4
"""

from enum import Enum
from typing import Any, Optional, List
from pydantic import BaseModel, Field, field_validator
from datetime import datetime


# 默认配额
BASIC__DEFAULT_QUOTA = 80
SUPER_DEFAULT_QUOTA = 140

# 失败阈值
FAIL_THRESHOLD = 5


class TokenStatus(str, Enum):
    """Token 状态"""

    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    COOLING = "cooling"
    BLACKLISTED = "blacklisted"


class EffortType(str, Enum):
    """请求消耗类型"""

    LOW = "low"  # 扣 1
    HIGH = "high"  # 扣 4


EFFORT_COST = {
    EffortType.LOW: 1,
    EffortType.HIGH: 4,
}


class TokenInfo(BaseModel):
    """Token 信息"""

    token: str
    status: TokenStatus = TokenStatus.ACTIVE
    quota: int = BASIC__DEFAULT_QUOTA

    # 消耗记录（本地累加，不依赖 API 返回值）
    # 仅在 consumed_mode_enabled=true 时使用
    consumed: int = 0

    # 统计
    created_at: int = Field(
        default_factory=lambda: int(datetime.now().timestamp() * 1000)
    )
    last_used_at: Optional[int] = None
    use_count: int = 0

    # 失败追踪
    fail_count: int = 0
    last_fail_at: Optional[int] = None
    last_fail_reason: Optional[str] = None

    # 冷却管理
    last_sync_at: Optional[int] = None  # 上次同步时间
    cooling_until: Optional[int] = None

    # 可用性检测
    alive: Optional[bool] = None  # None=未检测, True=可用, False=不可用
    last_alive_check_at: Optional[int] = None
    suspected_rate_limited_until: Optional[int] = None
    last_rate_limit_probe_at: Optional[int] = None
    last_rate_limit_probe_result: Optional[dict[str, Any]] = None
    bad_request_fail_count: int = 0
    last_bad_request_at: Optional[int] = None
    bad_request_cooling_until: Optional[int] = None
    blacklisted_at: Optional[int] = None
    delete_after_at: Optional[int] = None

    # 扩展
    tags: List[str] = Field(default_factory=list)
    note: str = ""
    email: Optional[str] = None
    last_asset_clear_at: Optional[int] = None
    real_tier: Optional[str] = None
    real_tier_name: Optional[str] = None
    real_quota: Optional[dict[str, Any]] = None
    last_real_quota_check_at: Optional[int] = None
    last_real_quota_error: Optional[str] = None

    @field_validator("token", mode="before")
    @classmethod
    def _normalize_token(cls, value):
        """Normalize copied tokens to avoid unicode punctuation issues."""
        if value is None:
            raise ValueError("token cannot be empty")
        token = str(value)
        token = token.translate(
            str.maketrans(
                {
                    "\u2010": "-",
                    "\u2011": "-",
                    "\u2012": "-",
                    "\u2013": "-",
                    "\u2014": "-",
                    "\u2212": "-",
                    "\u00a0": " ",
                    "\u2007": " ",
                    "\u202f": " ",
                    "\u200b": "",
                    "\u200c": "",
                    "\u200d": "",
                    "\ufeff": "",
                }
            )
        )
        token = "".join(token.split())
        if token.startswith("sso="):
            token = token[4:]
        token = token.encode("ascii", errors="ignore").decode("ascii")
        if not token:
            raise ValueError("token cannot be empty")
        return token

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value):
        if value is None:
            return None
        email = str(value).strip()
        if not email:
            return None
        return email.lower()

    def is_available(self, consumed_mode: bool = False) -> bool:
        """检查当前模式下 token 是否可用。"""
        if self.status != TokenStatus.ACTIVE:
            return False
        if self.alive is False:
            return False
        if self.is_soft_rate_limited():
            return False
        if self.is_bad_request_cooled():
            return False
        if consumed_mode:
            return True
        return self.quota > 0

    def is_bad_request_cooled(self, now_ms: Optional[int] = None) -> bool:
        if self.bad_request_cooling_until is None:
            return False
        if now_ms is None:
            now_ms = int(datetime.now().timestamp() * 1000)
        return now_ms < self.bad_request_cooling_until

    def is_soft_rate_limited(self, now_ms: Optional[int] = None) -> bool:
        """Whether this token is temporarily soft-cooled after an unconfirmed 429."""
        if self.suspected_rate_limited_until is None:
            return False
        if now_ms is None:
            now_ms = int(datetime.now().timestamp() * 1000)
        return now_ms < self.suspected_rate_limited_until

    def clear_soft_rate_limit(self):
        self.suspected_rate_limited_until = None

    def set_soft_rate_limit(self, until_ms: int):
        self.suspected_rate_limited_until = max(0, int(until_ms))

    def set_rate_limit_probe_result(
        self,
        payload: Optional[dict[str, Any]],
        *,
        checked_at: Optional[int] = None,
    ):
        now_ms = int(datetime.now().timestamp() * 1000)
        self.last_rate_limit_probe_at = now_ms if checked_at is None else int(checked_at)
        self.last_rate_limit_probe_result = dict(payload or {})

    def enter_cooling(
        self,
        reset_consumed: bool = True,
        *,
        until_ms: Optional[int] = None,
    ):
        """进入冷却状态，并在新窗口开始时清空 consumed。"""
        self.status = TokenStatus.COOLING
        self.cooling_until = None if until_ms is None else max(0, int(until_ms))
        self.clear_soft_rate_limit()
        if reset_consumed:
            self.consumed = 0

    def recover_active(
        self,
        allow_from_expired: bool = False,
        allow_from_blacklisted: bool = False,
    ):
        """仅在允许的前提下恢复为 active。"""
        if self.status == TokenStatus.COOLING:
            self.status = TokenStatus.ACTIVE
            self.cooling_until = None
        elif allow_from_expired and self.status == TokenStatus.EXPIRED:
            self.status = TokenStatus.ACTIVE
        elif allow_from_blacklisted and self.status == TokenStatus.BLACKLISTED:
            self.status = TokenStatus.ACTIVE
        self.cooling_until = None
        self.clear_soft_rate_limit()

    def clear_bad_request_state(self, *, clear_blacklist: bool = False):
        self.bad_request_fail_count = 0
        self.last_bad_request_at = None
        self.bad_request_cooling_until = None
        if clear_blacklist:
            self.blacklisted_at = None
            self.delete_after_at = None
            if self.status == TokenStatus.BLACKLISTED:
                self.status = TokenStatus.ACTIVE

    def record_bad_request(
        self,
        *,
        cooling_until_ms: Optional[int],
        blacklist_threshold: int,
        delete_after_ms: Optional[int],
    ) -> str:
        now_ms = int(datetime.now().timestamp() * 1000)
        if self.status == TokenStatus.BLACKLISTED:
            self.last_bad_request_at = now_ms
            if delete_after_ms is not None and self.delete_after_at is None:
                self.delete_after_at = max(0, int(delete_after_ms))
            return "blacklist"

        self.last_bad_request_at = now_ms
        self.bad_request_fail_count = max(0, int(self.bad_request_fail_count or 0)) + 1

        threshold = max(1, int(blacklist_threshold or 1))
        if self.bad_request_fail_count >= threshold:
            self.status = TokenStatus.BLACKLISTED
            self.bad_request_cooling_until = None
            self.blacklisted_at = now_ms
            self.delete_after_at = (
                None if delete_after_ms is None else max(0, int(delete_after_ms))
            )
            return "blacklist"

        self.bad_request_cooling_until = (
            None if cooling_until_ms is None else max(0, int(cooling_until_ms))
        )
        self.blacklisted_at = None
        self.delete_after_at = None
        return "quarantine"

    def recover_from_blacklist(self):
        self.clear_bad_request_state(clear_blacklist=True)
        self.recover_active(allow_from_expired=True, allow_from_blacklisted=True)

    def consume(self, effort: EffortType = EffortType.LOW) -> int:
        """
        消耗配额（默认：扣减 quota）

        Args:
            effort: LOW 计 1 次，HIGH 计 4 次

        Returns:
            实际扣除的配额
        """
        cost = EFFORT_COST[effort]

        # 默认行为：扣减 quota
        actual_cost = min(cost, self.quota)

        self.last_used_at = int(datetime.now().timestamp() * 1000)
        self.clear_soft_rate_limit()
        self.consumed += cost  # 无论是否开启消耗模式，都记录消耗
        self.use_count += actual_cost
        self.quota = max(0, self.quota - actual_cost)

        # 默认行为：quota 耗尽时标记冷却，并重置消耗记录
        if self.quota == 0:
            self.enter_cooling()
        else:
            self.recover_active()

        return actual_cost

    def consume_with_consumed(self, effort: EffortType = EffortType.LOW) -> int:
        """
        消耗配额（consumed 模式：累加 consumed 而非扣减 quota）

        仅在 consumed_mode_enabled=true 时使用

        Args:
            effort: LOW 计 1 次，HIGH 计 4 次

        Returns:
            实际计入的消耗次数
        """
        cost = EFFORT_COST[effort]

        self.clear_soft_rate_limit()
        self.consumed += cost  # 累加消耗记录
        self.last_used_at = int(datetime.now().timestamp() * 1000)
        self.use_count += 1

        # consumed 模式下不自动判断冷却，由 Rate Limits 检查或 429 触发
        self.recover_active()

        return cost

    def update_quota(self, new_quota: int):
        """
        更新配额（用于 API 同步 - 默认模式）

        Args:
            new_quota: 新的配额值
        """
        self.quota = max(0, new_quota)

        if self.quota == 0:
            self.enter_cooling()
        else:
            self.recover_active(allow_from_expired=True)

    def update_quota_with_consumed(self, new_quota: int):
        """
        更新配额（consumed 模式）

        仅在 consumed_mode_enabled=true 时使用

        Args:
            new_quota: 新的配额值
        """
        self.quota = max(0, new_quota)

        if self.quota == 0:
            self.enter_cooling()
        else:
            self.recover_active()

    def reset(self, default_quota: Optional[int] = None):
        """重置配额到默认值"""
        quota = BASIC__DEFAULT_QUOTA if default_quota is None else default_quota
        self.quota = max(0, int(quota))
        self.status = TokenStatus.ACTIVE
        self.cooling_until = None
        self.fail_count = 0
        self.last_fail_reason = None
        # 重置消耗记录
        self.consumed = 0
        self.clear_soft_rate_limit()
        self.clear_bad_request_state(clear_blacklist=True)
        self.last_rate_limit_probe_at = None
        self.last_rate_limit_probe_result = None

    def record_fail(
        self,
        status_code: int = 401,
        reason: str = "",
        threshold: Optional[int] = None,
    ):
        """记录失败，达到阈值后自动标记为 expired"""
        # 仅 401 计入失败
        if status_code != 401:
            return
        if self.status == TokenStatus.BLACKLISTED:
            return

        self.fail_count += 1
        self.last_fail_at = int(datetime.now().timestamp() * 1000)
        self.last_fail_reason = reason

        limit = FAIL_THRESHOLD if threshold is None else threshold
        if self.fail_count >= limit:
            self.status = TokenStatus.EXPIRED

    def record_success(self, is_usage: bool = True):
        """记录成功，清空失败计数"""
        self.fail_count = 0
        self.last_fail_at = None
        self.last_fail_reason = None
        self.cooling_until = None
        self.clear_soft_rate_limit()
        self.clear_bad_request_state()

        if is_usage:
            self.use_count += 1
            self.last_used_at = int(datetime.now().timestamp() * 1000)

    def need_refresh(self, interval_hours: int = 8) -> bool:
        """检查是否需要刷新配额"""
        if self.status != TokenStatus.COOLING:
            return False

        now = int(datetime.now().timestamp() * 1000)
        if self.cooling_until is not None:
            return now >= self.cooling_until

        if self.last_sync_at is None:
            return True

        interval_ms = interval_hours * 3600 * 1000
        return (now - self.last_sync_at) >= interval_ms

    def mark_synced(self):
        """标记已同步"""
        self.last_sync_at = int(datetime.now().timestamp() * 1000)

    def should_cool_down(self, remaining_tokens: int, threshold: int = 10) -> bool:
        """
        根据 Rate Limits 返回值判断是否应该冷却

        Args:
            remaining_tokens: API 返回的剩余配额
            threshold: 冷却阈值，默认 10

        Returns:
            是否应该进入冷却状态
        """
        if remaining_tokens <= threshold:
            self.status = TokenStatus.COOLING
            self.cooling_until = None
            return True
        return False


class TokenPoolStats(BaseModel):
    """Token 池统计"""

    total: int = 0
    active: int = 0
    disabled: int = 0
    expired: int = 0
    cooling: int = 0
    blacklisted: int = 0
    total_quota: int = 0
    avg_quota: float = 0.0
    total_consumed: int = 0
    avg_consumed: float = 0.0


__all__ = [
    "TokenStatus",
    "TokenInfo",
    "TokenPoolStats",
    "EffortType",
    "EFFORT_COST",
    "BASIC__DEFAULT_QUOTA",
    "SUPER_DEFAULT_QUOTA",
    "FAIL_THRESHOLD",
]
