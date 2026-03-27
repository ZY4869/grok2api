"""
Reverse interface: probe final image assets on assets.grok.com.
"""

from typing import Optional
from urllib.parse import urlparse

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
from app.services.reverse.utils.headers import build_ws_headers
from app.services.reverse.utils.retry import extract_status_for_retry, retry_on_status


class AppAssetReverse:
    """Probe assets.grok.com image URLs without downloading the whole file."""

    @staticmethod
    async def probe(session: AsyncSession, url: str) -> bool:
        if not url:
            return False

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False

        headers = build_ws_headers(origin="https://grok.com")
        headers.pop("Origin", None)
        headers["Accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        headers["Referer"] = "https://grok.com/"
        headers["Priority"] = "i"
        headers["Range"] = "bytes=0-0"
        headers["Sec-Fetch-Dest"] = "image"
        headers["Sec-Fetch-Mode"] = "no-cors"
        headers["Sec-Fetch-Site"] = "same-site"

        timeout = float(get_config("asset.download_timeout") or get_config("chat.timeout") or 60)
        browser = get_config("proxy.browser")
        active_proxy_key = None

        async def _do_request():
            nonlocal active_proxy_key
            active_proxy_key, proxy_url = get_current_proxy_from(
                "proxy.asset_proxy_url",
                "proxy.base_proxy_url",
            )
            proxies = build_http_proxies(proxy_url)
            return await session.get(
                url,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                allow_redirects=True,
                impersonate=browser,
            )

        def _extract_status(error: Exception) -> Optional[int]:
            status = extract_status_for_retry(error)
            if status == 429:
                return None
            return status

        async def _on_retry(
            attempt: int, status_code: int, error: Exception, delay: float
        ):
            if active_proxy_key and should_rotate_proxy(status_code):
                rotate_proxy(active_proxy_key)

        try:
            response = await retry_on_status(
                _do_request,
                extract_status=_extract_status,
                on_retry=_on_retry,
            )
        except Exception as exc:
            if isinstance(exc, UpstreamException):
                raise
            raise UpstreamException(
                message=f"AppAssetReverse: probe failed, {exc}",
                details={"status": 502, "error": str(exc)},
            ) from exc

        content_type = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code not in {200, 206}:
            logger.debug(
                "Quick image asset probe pending",
                extra={
                    "url": url,
                    "status": status_code,
                    "content_type": content_type,
                },
            )
            return False

        return content_type.startswith("image/")


__all__ = ["AppAssetReverse"]
