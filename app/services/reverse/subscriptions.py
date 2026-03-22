"""
Reverse interface: subscriptions.
"""

from typing import Any

from curl_cffi.requests import AsyncSession

from app.core.config import get_config
from app.core.exceptions import UpstreamException
from app.core.logger import logger
from app.core.proxy_pool import (
    build_http_proxies,
    get_current_proxy_from,
    rotate_proxy,
    should_rotate_proxy,
)
from app.services.reverse.utils.headers import build_headers
from app.services.reverse.utils.retry import retry_on_status

SUBSCRIPTIONS_API = "https://grok.com/rest/subscriptions"


class SubscriptionsReverse:
    """/rest/subscriptions reverse interface."""

    @staticmethod
    async def request(session: AsyncSession, token: str) -> Any:
        try:
            headers = build_headers(
                cookie_token=token,
                origin="https://grok.com",
                referer="https://grok.com/",
            )

            timeout = get_config("usage.timeout")
            browser = get_config("proxy.browser")
            active_proxy_key = None

            async def _do_request():
                nonlocal active_proxy_key
                active_proxy_key, proxy_url = get_current_proxy_from(
                    "proxy.base_proxy_url"
                )
                proxies = build_http_proxies(proxy_url)
                response = await session.get(
                    SUBSCRIPTIONS_API,
                    headers=headers,
                    timeout=timeout,
                    proxies=proxies,
                    impersonate=browser,
                )

                if response.status_code != 200:
                    try:
                        resp_text = response.text
                    except Exception:
                        resp_text = "N/A"
                    raise UpstreamException(
                        message=(
                            f"SubscriptionsReverse: Request failed, {response.status_code}"
                        ),
                        details={"status": response.status_code, "body": resp_text},
                    )

                return response

            async def _on_retry(
                attempt: int, status_code: int, error: Exception, delay: float
            ):
                if active_proxy_key and should_rotate_proxy(status_code):
                    rotate_proxy(active_proxy_key)

            return await retry_on_status(_do_request, on_retry=_on_retry)

        except Exception as e:
            if isinstance(e, UpstreamException):
                raise

            logger.error(f"SubscriptionsReverse: Request failed, {type(e).__name__}: {e}")
            raise UpstreamException(
                message=f"SubscriptionsReverse: Request failed, {e}",
                details={"status": 502, "error": str(e)},
            )


__all__ = ["SubscriptionsReverse"]
