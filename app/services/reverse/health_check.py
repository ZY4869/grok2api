"""
Reverse interface: token health check (zero-cost).
Uses /rest/assets as a lightweight probe — no quota consumed.
"""

from enum import Enum
from curl_cffi.requests import AsyncSession

from app.core.logger import logger
from app.core.config import get_config
from app.core.proxy_pool import build_http_proxies, get_current_proxy_from
from app.services.reverse.utils.headers import build_headers

HEALTH_CHECK_API = "https://grok.com/rest/assets"


class AliveStatus(str, Enum):
    ALIVE = "alive"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class HealthCheckReverse:
    """Lightweight token health check via /rest/assets (no quota cost)."""

    @staticmethod
    async def check(session: AsyncSession, token: str) -> AliveStatus:
        """Check if a token is alive without consuming quota.

        Returns:
            AliveStatus: alive / expired / unknown
        """
        try:
            headers = build_headers(
                cookie_token=token,
                content_type="application/json",
                origin="https://grok.com",
                referer="https://grok.com/",
            )

            timeout = get_config("asset.list_timeout") or 15
            browser = get_config("proxy.browser")
            _, proxy_url = get_current_proxy_from(
                "proxy.base_proxy_url",
            )
            proxies = build_http_proxies(proxy_url)

            response = await session.get(
                HEALTH_CHECK_API,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                impersonate=browser,
            )

            if response.status_code == 200:
                return AliveStatus.ALIVE
            elif response.status_code == 401:
                return AliveStatus.EXPIRED
            else:
                logger.warning(
                    "HealthCheck: unexpected status={}, body={}",
                    response.status_code,
                    response.text[:200] if response.text else "N/A",
                )
                return AliveStatus.UNKNOWN

        except Exception as e:
            logger.warning(f"HealthCheck: request failed, {type(e).__name__}: {e}")
            return AliveStatus.UNKNOWN


__all__ = ["HealthCheckReverse", "AliveStatus"]
