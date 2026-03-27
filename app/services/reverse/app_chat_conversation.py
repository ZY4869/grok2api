"""
Reverse interface: app chat conversations_v2 polling.
"""

from __future__ import annotations

from typing import Any, Optional

import orjson
from curl_cffi.requests import AsyncSession

from app.core.config import get_config
from app.core.exceptions import UpstreamException
from app.core.logger import logger
from app.services.reverse.app_chat import _normalize_chat_proxy
from app.services.reverse.utils.headers import build_headers
from app.services.reverse.utils.retry import extract_status_for_retry, retry_on_status
from app.core.proxy_pool import (
    get_current_proxy_from,
    rotate_proxy,
    should_rotate_proxy,
)


class AppChatConversationReverse:
    """Reverse interface for app-chat conversations_v2."""

    @staticmethod
    async def request(
        session: AsyncSession,
        token: str,
        conversation_id: str,
    ) -> Any:
        if not conversation_id:
            raise UpstreamException(
                message="Conversation id is required",
                details={"status": 400, "error": "missing_conversation_id"},
            )

        url = (
            "https://grok.com/rest/app-chat/conversations_v2/"
            f"{conversation_id}?includeWorkspaces=true&includeTaskResult=true"
        )
        headers = build_headers(
            cookie_token=token,
            origin="https://grok.com",
            referer=f"https://grok.com/c/{conversation_id}",
        )
        timeout = float(get_config("chat.timeout") or 60)
        browser = get_config("proxy.browser")
        active_proxy_key = None

        async def _do_request():
            nonlocal active_proxy_key
            active_proxy_key, base_proxy = get_current_proxy_from("proxy.base_proxy_url")
            proxy = None
            proxies = None
            if base_proxy:
                normalized_proxy = _normalize_chat_proxy(base_proxy)
                if normalized_proxy.lower().startswith(("socks4", "socks5")):
                    proxy = normalized_proxy
                else:
                    proxies = {
                        "http": normalized_proxy,
                        "https": normalized_proxy,
                    }

            response = await session.get(
                url,
                headers=headers,
                timeout=timeout,
                proxy=proxy,
                proxies=proxies,
                impersonate=browser,
            )
            if response.status_code != 200:
                body = ""
                try:
                    body = await response.text()
                except Exception:
                    pass
                raise UpstreamException(
                    message=(
                        "AppChatConversationReverse: conversations_v2 failed, "
                        f"{response.status_code}"
                    ),
                    details={
                        "status": response.status_code,
                        "body": body,
                    },
                )
            return response

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

        response = await retry_on_status(
            _do_request,
            extract_status=_extract_status,
            on_retry=_on_retry,
        )
        try:
            return response.json()
        except Exception:
            text = await response.text()
            logger.warning(
                "AppChatConversationReverse returned non-json payload",
                extra={
                    "conversation_id": conversation_id,
                    "payload_preview": text[:200],
                },
            )
            try:
                return orjson.loads(text)
            except orjson.JSONDecodeError as exc:
                raise UpstreamException(
                    message="AppChatConversationReverse: invalid json payload",
                    details={"status": 502, "error": str(exc)},
                ) from exc


__all__ = ["AppChatConversationReverse"]
