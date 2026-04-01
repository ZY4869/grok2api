"""
Reverse interface: app chat conversations.
"""

import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import urlparse

import orjson
from curl_cffi.requests import AsyncSession

from app.core.config import get_config
from app.core.exceptions import UpstreamException
from app.core.logger import logger
from app.services.grok.utils.prompt_debug import summarize_prompt_text
from app.core.proxy_pool import (
    get_current_proxy_from,
    rotate_proxy,
    should_rotate_proxy,
)
from app.services.reverse.utils.headers import build_headers
from app.services.reverse.utils.retry import extract_status_for_retry, retry_on_status
from app.services.token.service import TokenService

CHAT_API = "https://grok.com/rest/app-chat/conversations/new"
APP_CHAT_REQUEST_MODE_ID = "mode_id"
APP_CHAT_REQUEST_LEGACY_MODEL = "legacy_model"
APP_CHAT_REQUEST_MODEL_ID_AUTO = "model_id_auto"
_CONVERSATION_ID_PATTERNS = (
    re.compile(r"/c/([0-9a-fA-F-]{36})"),
    re.compile(r"conversationId=([0-9a-fA-F-]{36})"),
    re.compile(r"/conversations_v2/([0-9a-fA-F-]{36})"),
)
_DIRECT_CONVERSATION_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")
_RESPONSE_ID_PATTERNS = (
    re.compile(r"responseId[\"'=:\s]+([0-9a-fA-F-]{8,64})"),
    re.compile(r"\brid=([0-9a-fA-F-]{8,64})"),
)
_DEFAULT_TOOL_OVERRIDES = {
    "gmailSearch": False,
    "googleCalendarSearch": False,
    "outlookSearch": False,
    "outlookCalendarSearch": False,
    "googleDriveSearch": False,
}
_ERROR_BODY_LOG_LIMIT = 240


@dataclass
class AppChatRequestMetadata:
    conversation_id: Optional[str] = None
    response_id: Optional[str] = None
    response_url: str = ""
    response_headers: Dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0


@dataclass
class AppChatRequestResult:
    stream: AsyncGenerator[str, None]
    metadata: AppChatRequestMetadata

    def __aiter__(self):
        return self.stream.__aiter__()


def _merge_request_overrides(
    base: Dict[str, Any], overrides: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Recursively merge request-level overrides into the payload."""
    if not overrides:
        return base

    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_request_overrides(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_chat_proxy(proxy_url: str) -> str:
    """Normalize proxy URL for curl-cffi app-chat requests."""
    if not proxy_url:
        return proxy_url
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme == "socks5":
        return proxy_url.replace("socks5://", "socks5h://", 1)
    if scheme == "socks4":
        return proxy_url.replace("socks4://", "socks4a://", 1)
    return proxy_url


def _extract_pattern_match(
    patterns: tuple[re.Pattern[str], ...], value: str
) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            found = (match.group(1) or "").strip()
            if found:
                return found
    return None


def _extract_conversation_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if _DIRECT_CONVERSATION_ID_PATTERN.fullmatch(text):
        return text
    return _extract_pattern_match(_CONVERSATION_ID_PATTERNS, text)


def _update_metadata_from_value(metadata: AppChatRequestMetadata, value: Any) -> None:
    if value is None:
        return

    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key or "").strip().lower()
            if not metadata.conversation_id and key_text in {
                "conversationid",
                "conversation_id",
            }:
                match = _extract_conversation_id(item)
                if match:
                    metadata.conversation_id = match
            if not metadata.response_id and key_text in {"responseid", "response_id"}:
                response_id = str(item or "").strip()
                if response_id:
                    metadata.response_id = response_id
            _update_metadata_from_value(metadata, item)
        return

    if isinstance(value, list):
        for item in value:
            _update_metadata_from_value(metadata, item)
        return

    if isinstance(value, str):
        if not metadata.conversation_id:
            match = _extract_pattern_match(_CONVERSATION_ID_PATTERNS, value)
            if match:
                metadata.conversation_id = match
        if not metadata.response_id:
            match = _extract_pattern_match(_RESPONSE_ID_PATTERNS, value)
            if match:
                metadata.response_id = match


def _update_metadata_from_line(
    metadata: AppChatRequestMetadata, raw_line: Any
) -> None:
    if raw_line is None:
        return

    if isinstance(raw_line, (bytes, bytearray)):
        text = raw_line.decode("utf-8", errors="ignore").strip()
    else:
        text = str(raw_line).strip()
    if not text:
        return
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text or text == "[DONE]":
        return

    try:
        payload = orjson.loads(text)
    except orjson.JSONDecodeError:
        _update_metadata_from_value(metadata, text)
        return

    _update_metadata_from_value(metadata, payload)


def _resolve_request_strategy(
    use_mode_id: bool, request_strategy: Optional[str]
) -> str:
    """Resolve the upstream request shape for app-chat."""
    if request_strategy:
        return request_strategy
    if use_mode_id:
        return APP_CHAT_REQUEST_MODE_ID
    return APP_CHAT_REQUEST_LEGACY_MODEL


def _summarize_error_body(content: str, limit: int = _ERROR_BODY_LOG_LIMIT) -> str:
    text = str(content or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


class AppChatReverse:
    """/rest/app-chat/conversations/new reverse interface."""

    @staticmethod
    def _resolve_custom_personality() -> Optional[str]:
        """Resolve optional custom personality from app config."""
        value = get_config("app.custom_instruction", "")
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        if not value.strip():
            return None
        return value

    @staticmethod
    def build_payload(
        message: str,
        model: str,
        mode: str = None,
        file_attachments: List[str] = None,
        request_overrides: Dict[str, Any] = None,
        tool_overrides: Dict[str, Any] = None,
        model_config_override: Dict[str, Any] = None,
        use_mode_id: bool = False,
        request_strategy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build chat payload for Grok app-chat API."""

        attachments = file_attachments or []
        strategy = _resolve_request_strategy(use_mode_id, request_strategy)

        payload = {
            "deviceEnvInfo": {
                "darkModeEnabled": False,
                "devicePixelRatio": 2,
                "screenHeight": 1329,
                "screenWidth": 2056,
                "viewportHeight": 1083,
                "viewportWidth": 2056,
            },
            "disableMemory": get_config("app.disable_memory"),
            "disableSearch": False,
            "disableSelfHarmShortCircuit": False,
            "disableTextFollowUps": False,
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "enableSideBySide": True,
            "fileAttachments": attachments,
            "forceConcise": False,
            "forceSideBySide": False,
            "imageAttachments": [],
            "imageGenerationCount": 2,
            "isAsyncChat": False,
            "message": message,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "sendFinalMetadata": True,
            "temporary": get_config("app.temporary"),
            "toolOverrides": deepcopy(_DEFAULT_TOOL_OVERRIDES),
        }

        if strategy == APP_CHAT_REQUEST_MODE_ID:
            payload["collectionIds"] = []
            payload["connectors"] = []
            payload["searchAllConnectors"] = False
            payload["modeId"] = mode
            payload["enable420"] = bool(get_config("app.auto_enable_420", True))
            payload["responseMetadata"] = {}
        elif strategy == APP_CHAT_REQUEST_MODEL_ID_AUTO:
            payload["responseMetadata"] = {
                "requestModelDetails": {"modelId": "auto"},
            }
        else:
            payload["modelName"] = model
            payload["modelMode"] = mode
            payload["responseMetadata"] = {
                "requestModelDetails": {"modelId": model},
            }

        custom_personality = AppChatReverse._resolve_custom_personality()
        if custom_personality is not None:
            payload["customPersonality"] = custom_personality

        payload = _merge_request_overrides(payload, request_overrides)
        merged_tool_overrides = _merge_request_overrides(
            deepcopy(_DEFAULT_TOOL_OVERRIDES),
            payload.get("toolOverrides") if isinstance(payload.get("toolOverrides"), dict) else {},
        )
        if tool_overrides:
            merged_tool_overrides = _merge_request_overrides(
                merged_tool_overrides,
                tool_overrides,
            )
        payload["toolOverrides"] = merged_tool_overrides

        # For mode_id strategy (auto mode), skip modelConfigOverride to keep
        # responseMetadata empty — matching browser behavior.  Adding config
        # overrides causes Grok to treat the request as text-only, suppressing
        # image generation in auto mode.
        if model_config_override and strategy != APP_CHAT_REQUEST_MODE_ID:
            payload.setdefault("responseMetadata", {})
            payload["responseMetadata"]["modelConfigOverride"] = model_config_override

        import json

        logger.debug(
            f"AppChatReverse payload: {json.dumps(payload, indent=4, ensure_ascii=False)}"
        )

        return payload

    @staticmethod
    async def request(
        session: AsyncSession,
        token: str,
        message: str,
        model: str,
        mode: str = None,
        file_attachments: List[str] = None,
        request_overrides: Dict[str, Any] = None,
        tool_overrides: Dict[str, Any] = None,
        model_config_override: Dict[str, Any] = None,
        use_mode_id: bool = False,
        request_strategy: Optional[str] = None,
    ) -> AppChatRequestResult:
        """Send app chat request to Grok."""
        try:
            headers = build_headers(
                cookie_token=token,
                content_type="application/json",
                origin="https://grok.com",
                referer="https://grok.com/",
            )

            payload = AppChatReverse.build_payload(
                message=message,
                model=model,
                mode=mode,
                file_attachments=file_attachments,
                request_overrides=request_overrides,
                tool_overrides=tool_overrides,
                model_config_override=model_config_override,
                use_mode_id=use_mode_id,
                request_strategy=request_strategy,
            )
            request_model_details = (payload.get("responseMetadata") or {}).get(
                "requestModelDetails", {}
            )
            payload_summary = {
                "model": payload.get("modelName"),
                "mode": payload.get("modelMode"),
                "mode_id": payload.get("modeId"),
                "enable_420": payload.get("enable420"),
                "request_strategy": _resolve_request_strategy(
                    use_mode_id, request_strategy
                ),
                "request_model_id": request_model_details.get("modelId"),
                "image_generation_count": payload.get("imageGenerationCount"),
                "enable_nsfw": payload.get("enableNsfw"),
                "message_len": len(payload.get("message") or ""),
                "message_summary": summarize_prompt_text(payload.get("message") or ""),
                "file_attachments": len(payload.get("fileAttachments") or []),
                "tool_override_keys": sorted((payload.get("toolOverrides") or {}).keys()),
                "collection_ids_count": len(payload.get("collectionIds") or []),
                "connector_count": len(payload.get("connectors") or []),
                "search_all_connectors": bool(payload.get("searchAllConnectors")),
                "custom_personality_len": len(payload.get("customPersonality") or ""),
            }
            logger.debug(
                "AppChatReverse final Grok params (redacted)",
                extra={"grok_payload": payload_summary},
            )

            timeout = float(get_config("chat.timeout") or 0)
            if timeout <= 0:
                timeout = max(
                    float(get_config("video.timeout") or 0),
                    float(get_config("image.timeout") or 0),
                )
            browser = get_config("proxy.browser")
            active_proxy_key = None
            metadata = AppChatRequestMetadata(started_at=time.monotonic())

            async def _do_request():
                nonlocal active_proxy_key
                active_proxy_key, base_proxy = get_current_proxy_from(
                    "proxy.base_proxy_url"
                )
                proxy = None
                proxies = None
                if base_proxy:
                    normalized_proxy = _normalize_chat_proxy(base_proxy)
                    scheme = urlparse(normalized_proxy).scheme.lower()
                    if scheme.startswith("socks"):
                        proxy = normalized_proxy
                    else:
                        proxies = {
                            "http": normalized_proxy,
                            "https": normalized_proxy,
                        }
                    logger.info(
                        f"AppChatReverse proxy enabled: scheme={scheme}, target={normalized_proxy}"
                    )
                else:
                    logger.warning(
                        "AppChatReverse proxy is empty, request will use direct network"
                    )
                response = await session.post(
                    CHAT_API,
                    headers=headers,
                    data=orjson.dumps(payload),
                    timeout=timeout,
                    stream=True,
                    proxy=proxy,
                    proxies=proxies,
                    impersonate=browser,
                )

                if response.status_code != 200:
                    content = ""
                    try:
                        content = await response.text()
                    except Exception:
                        pass
                    body_excerpt = _summarize_error_body(content)

                    logger.debug(
                        "AppChatReverse: Chat failed response body: %s",
                        body_excerpt,
                    )
                    logger.error(
                        f"AppChatReverse: Chat failed, {response.status_code}",
                        extra={
                            "error_type": "UpstreamException",
                            "status_code": response.status_code,
                            "body_excerpt": body_excerpt,
                            "body_size": len(content or ""),
                        },
                    )
                    raise UpstreamException(
                        message=f"AppChatReverse: Chat failed, {response.status_code}",
                        details={
                            "status": response.status_code,
                            "body": body_excerpt,
                            "body_excerpt": body_excerpt,
                            "body_size": len(content or ""),
                        },
                    )

                return response

            def extract_status(e: Exception) -> Optional[int]:
                status = extract_status_for_retry(e)
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
                extract_status=extract_status,
                on_retry=_on_retry,
            )

            metadata.response_url = str(getattr(response, "url", "") or "")
            try:
                metadata.response_headers = {
                    str(k): str(v)
                    for k, v in dict(getattr(response, "headers", {}) or {}).items()
                }
            except Exception:
                metadata.response_headers = {}
            _update_metadata_from_value(metadata, metadata.response_url)
            _update_metadata_from_value(metadata, metadata.response_headers)

            async def stream_response():
                _line_count = 0
                try:
                    async for line in response.aiter_lines():
                        _line_count += 1
                        if _line_count <= 15:
                            raw_preview = str(line)[:1200] if line else ""
                            logger.info(
                                f"AppChatReverse raw line#{_line_count}: {raw_preview}"
                            )
                        _update_metadata_from_line(metadata, line)
                        yield line
                finally:
                    logger.info(
                        f"AppChatReverse stream ended: lines={_line_count} conv={metadata.conversation_id or ''} resp={metadata.response_id or ''}"
                    )
                    await session.close()

            return AppChatRequestResult(stream=stream_response(), metadata=metadata)

        except Exception as e:
            if isinstance(e, UpstreamException):
                if e.details and "status" in e.details:
                    status = e.details["status"]
                else:
                    status = getattr(e, "status_code", None)
                if status == 401:
                    try:
                        await TokenService.record_fail(
                            token, status, "app_chat_auth_failed"
                        )
                    except Exception:
                        pass
                raise

            logger.error(
                f"AppChatReverse: Chat failed, {str(e)}",
                extra={"error_type": type(e).__name__},
            )
            raise UpstreamException(
                message=f"AppChatReverse: Chat failed, {str(e)}",
                details={"status": 502, "error": str(e)},
            )


__all__ = [
    "APP_CHAT_REQUEST_LEGACY_MODEL",
    "APP_CHAT_REQUEST_MODEL_ID_AUTO",
    "APP_CHAT_REQUEST_MODE_ID",
    "AppChatRequestMetadata",
    "AppChatRequestResult",
    "AppChatReverse",
]
