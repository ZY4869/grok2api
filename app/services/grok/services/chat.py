"""
Grok Chat 服务
"""

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Any, AsyncGenerator, AsyncIterable, Optional
from urllib.parse import urlsplit, urlunsplit

import orjson
from curl_cffi.requests.errors import RequestsError

from app.core.logger import logger
from app.core.config import get_config
from app.core.exceptions import (
    AppException,
    ValidationException,
    ErrorType,
    UpstreamException,
    StreamIdleTimeoutError,
)
from app.services.grok.services.model import ModelService
from app.services.grok.utils.upload import UploadService
from app.services.grok.utils import process as proc_base
from app.services.grok.utils.prompt_debug import (
    detect_image_keyword_categories as _detect_image_keyword_categories,
    extract_last_user_text as _extract_last_user_text_for_summary,
    looks_like_image_prompt as _looks_like_image_prompt_text,
    summarize_chat_messages,
    summarize_prompt_text,
)
from app.services.grok.utils.retry import pick_token, rate_limited, transient_upstream
from app.services.reverse.app_chat import (
    AppChatRequestMetadata,
    AppChatRequestResult,
    AppChatReverse,
)
from app.services.reverse.app_asset import AppAssetReverse
from app.services.reverse.app_chat_conversation import AppChatConversationReverse
from app.services.reverse.utils.session import ResettableSession
from app.services.grok.utils.stream import wrap_stream_with_usage
from app.services.grok.utils.tool_call import (
    build_tool_prompt,
    parse_tool_calls,
    parse_tool_call_block,
    format_tool_history,
)
from app.services.token.model_access import (
    model_access_denied_error,
    model_requires_special_subscription,
)
from app.services.token import get_token_manager, EffortType
from app.services.token.quota import (
    RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED,
    RATE_LIMIT_ACTION_RETRY_SAME_TOKEN,
    all_candidate_tokens_exhausted,
    confirm_quota_exhausted,
    image_quota_requirement,
    image_limit_exception,
    resolve_rate_limit_hit,
    select_token_for_requirement,
)


_CHAT_SEMAPHORE = None
_CHAT_SEM_VALUE = None
_QUICK_IMAGE_MODELS = frozenset({"grok-auto", "grok-3-fast", "grok-4-expert"})
_QUICK_IMAGE_POLL_INTERVAL_SEC = 1.0
_QUICK_IMAGE_PREVIEW_URL_RE = re.compile(r"/generated/[^/]+-part-\d+/")
_QUICK_IMAGE_PREVIEW_SEGMENT_RE = re.compile(r"-part-\d+(?=/)")
_QUICK_IMAGE_TOOL_SIGNAL_KEYS = frozenset(
    {"toolCall", "toolCalls", "tool_calls", "toolResult", "toolResults"}
)
_QUICK_IMAGE_STATE_WAIT_SHORT_TEXT_LIMIT = 24
_QUICK_IMAGE_STATE_WAIT_MAX_SEC = 3.0


def extract_tool_text(raw: str, rollout_id: str = "") -> str:
    if not raw:
        return ""
    name_match = re.search(
        r"<xai:tool_name>(.*?)</xai:tool_name>", raw, flags=re.DOTALL
    )
    args_match = re.search(
        r"<xai:tool_args>(.*?)</xai:tool_args>", raw, flags=re.DOTALL
    )

    name = name_match.group(1) if name_match else ""
    if name:
        name = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", name, flags=re.DOTALL).strip()

    args = args_match.group(1) if args_match else ""
    if args:
        args = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", args, flags=re.DOTALL).strip()

    payload = None
    if args:
        try:
            payload = orjson.loads(args)
        except orjson.JSONDecodeError:
            payload = None

    label = name
    text = args
    prefix = f"[{rollout_id}]" if rollout_id else ""

    if name == "web_search":
        label = f"{prefix}[WebSearch]"
        if isinstance(payload, dict):
            text = payload.get("query") or payload.get("q") or ""
    elif name == "search_images":
        label = f"{prefix}[SearchImage]"
        if isinstance(payload, dict):
            text = (
                payload.get("image_description")
                or payload.get("description")
                or payload.get("query")
                or ""
            )
    elif name == "chatroom_send":
        label = f"{prefix}[AgentThink]"
        if isinstance(payload, dict):
            text = payload.get("message") or ""

    if label and text:
        return f"{label} {text}".strip()
    if label:
        return label
    if text:
        return text
    # Fallback: strip tags to keep any raw text.
    return re.sub(r"<[^>]+>", "", raw, flags=re.DOTALL).strip()


def _get_chat_semaphore() -> asyncio.Semaphore:
    global _CHAT_SEMAPHORE, _CHAT_SEM_VALUE
    value = max(1, int(get_config("chat.concurrent")))
    if value != _CHAT_SEM_VALUE:
        _CHAT_SEM_VALUE = value
        _CHAT_SEMAPHORE = asyncio.Semaphore(value)
    return _CHAT_SEMAPHORE


class MessageExtractor:
    """消息内容提取器"""

    @staticmethod
    def extract(
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        tool_choice: Any = None,
        parallel_tool_calls: bool = True,
    ) -> tuple[str, List[str], List[str]]:
        """从 OpenAI 消息格式提取内容，返回 (text, file_attachments, image_attachments)"""
        # Pre-process: convert tool-related messages to text format
        if tools:
            messages = format_tool_history(messages)

        texts = []
        file_attachments: List[str] = []
        image_attachments: List[str] = []
        extracted = []

        for msg in messages:
            role = msg.get("role", "") or "user"
            content = msg.get("content", "")
            parts = []

            if isinstance(content, str):
                if content.strip():
                    parts.append(content)
            elif isinstance(content, dict):
                content = [content]
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")
                    if item_type == "text":
                        if text := item.get("text", "").strip():
                            parts.append(text)
                    elif item_type == "image_url":
                        image_data = item.get("image_url", {})
                        url = image_data.get("url", "")
                        if url:
                            image_attachments.append(url)
                    elif item_type == "input_audio":
                        audio_data = item.get("input_audio", {})
                        data = audio_data.get("data", "")
                        if data:
                            file_attachments.append(data)
                    elif item_type == "file":
                        file_data = item.get("file", {})
                        raw = file_data.get("file_data", "")
                        if raw:
                            file_attachments.append(raw)
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")

                    if item_type == "text":
                        if text := item.get("text", "").strip():
                            parts.append(text)

                    elif item_type == "image_url":
                        image_data = item.get("image_url", {})
                        url = image_data.get("url", "")
                        if url:
                            image_attachments.append(url)

                    elif item_type == "input_audio":
                        audio_data = item.get("input_audio", {})
                        data = audio_data.get("data", "")
                        if data:
                            file_attachments.append(data)

                    elif item_type == "file":
                        file_data = item.get("file", {})
                        raw = file_data.get("file_data", "")
                        if raw:
                            file_attachments.append(raw)

            # 保留工具调用轨迹，避免部分客户端在多轮工具会话中丢失上下文顺序
            tool_calls = msg.get("tool_calls")
            if role == "assistant" and not parts and isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function", {})
                    if not isinstance(fn, dict):
                        fn = {}
                    name = fn.get("name") or call.get("name") or "tool"
                    arguments = fn.get("arguments", "")
                    if isinstance(arguments, (dict, list)):
                        try:
                            arguments = orjson.dumps(arguments).decode()
                        except Exception:
                            arguments = str(arguments)
                    if not isinstance(arguments, str):
                        arguments = str(arguments)
                    arguments = arguments.strip()
                    parts.append(
                        f"[tool_call] {name} {arguments}".strip()
                    )

            if parts:
                role_label = role
                if role == "tool":
                    name = msg.get("name")
                    call_id = msg.get("tool_call_id")
                    if isinstance(name, str) and name.strip():
                        role_label = f"tool[{name.strip()}]"
                    if isinstance(call_id, str) and call_id.strip():
                        role_label = f"{role_label}#{call_id.strip()}"
                extracted.append({"role": role_label, "text": "\n".join(parts)})

        # 找到最后一条 user 消息
        last_user_index = next(
            (
                i
                for i in range(len(extracted) - 1, -1, -1)
                if extracted[i]["role"] == "user"
            ),
            None,
        )

        for i, item in enumerate(extracted):
            role = item["role"] or "user"
            text = item["text"]
            texts.append(text if i == last_user_index else f"{role}: {text}")

        combined = "\n\n".join(texts)

        # If there are attachments but no text, inject a fallback prompt.
        if (not combined.strip()) and (file_attachments or image_attachments):
            combined = "Refer to the following content:"

        # Prepend tool system prompt if tools are provided
        if tools:
            tool_prompt = build_tool_prompt(tools, tool_choice, parallel_tool_calls)
            if tool_prompt:
                combined = f"{tool_prompt}\n\n{combined}"

        logger.debug(
            "MessageExtractor extracted prompt summary",
            extra={
                "prompt_summary": summarize_prompt_text(combined),
                "source_prompt_summary": summarize_chat_messages(messages),
                "file_attachment_count": len(file_attachments),
                "image_attachment_count": len(image_attachments),
            },
        )

        return combined, file_attachments, image_attachments


class GrokChatService:
    """Grok API 调用服务"""

    async def chat_request(
        self,
        token: str,
        message: str,
        model: str,
        mode: str = None,
        stream: bool = None,
        file_attachments: List[str] = None,
        request_overrides: Dict[str, Any] = None,
        tool_overrides: Dict[str, Any] = None,
        model_config_override: Dict[str, Any] = None,
        use_mode_id: bool = False,
        request_strategy: str | None = None,
    ):
        """发送聊天请求"""
        if stream is None:
            stream = get_config("app.stream")

        logger.debug(
            f"Chat request: model={model}, mode={mode}, stream={stream}, use_mode_id={use_mode_id}, request_strategy={request_strategy}, attachments={len(file_attachments or [])}"
        )

        browser = get_config("proxy.browser")
        semaphore = _get_chat_semaphore()
        await semaphore.acquire()
        session = ResettableSession(impersonate=browser)
        try:
            request_result = await AppChatReverse.request(
                session,
                token,
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
            logger.info(f"Chat connected: model={model}, stream={stream}")
        except Exception:
            try:
                await session.close()
            except Exception:
                pass
            semaphore.release()
            raise

        if not isinstance(request_result, AppChatRequestResult):
            request_result = AppChatRequestResult(
                stream=request_result,
                metadata=AppChatRequestMetadata(),
            )

        async def _stream():
            try:
                async for line in request_result:
                    yield line
            finally:
                semaphore.release()

        return AppChatRequestResult(
            stream=_stream(),
            metadata=request_result.metadata,
        )

    async def chat(
        self,
        token: str,
        message: str,
        model: str,
        mode: str = None,
        stream: bool = None,
        file_attachments: List[str] = None,
        request_overrides: Dict[str, Any] = None,
        tool_overrides: Dict[str, Any] = None,
        model_config_override: Dict[str, Any] = None,
        use_mode_id: bool = False,
        request_strategy: str | None = None,
    ):
        return await self.chat_request(
            token=token,
            message=message,
            model=model,
            mode=mode,
            stream=stream,
            file_attachments=file_attachments,
            request_overrides=request_overrides,
            tool_overrides=tool_overrides,
            model_config_override=model_config_override,
            use_mode_id=use_mode_id,
            request_strategy=request_strategy,
        )

    async def chat_openai(
        self,
        token: str,
        model: str,
        messages: List[Dict[str, Any]],
        stream: bool = None,
        reasoning_effort: str | None = None,
        temperature: float = 0.8,
        top_p: float = 0.95,
        tools: List[Dict[str, Any]] = None,
        tool_choice: Any = None,
        parallel_tool_calls: bool = True,
    ):
        """OpenAI 兼容接口"""
        model_info = ModelService.get(model)
        if not model_info:
            raise ValidationException(f"Unknown model: {model}")

        grok_model = model_info.grok_model
        mode = model_info.model_mode
        use_mode_id = model_info.use_mode_id
        # 提取消息和附件
        message, file_attachments, image_attachments = MessageExtractor.extract(
            messages, tools=tools, tool_choice=tool_choice, parallel_tool_calls=parallel_tool_calls
        )
        source_prompt_summary = summarize_chat_messages(messages)
        upstream_prompt_summary = summarize_prompt_text(message)
        image_keyword_categories = _detect_image_keyword_categories(
            _extract_last_user_text(messages)
        )
        logger.debug(
            "Extracted message length=%s, files=%s, images=%s",
            len(message),
            len(file_attachments),
            len(image_attachments),
        )
        logger.debug(
            "Chat upstream prompt summary",
            extra={
                "model": model,
                "source_prompt_summary": source_prompt_summary,
                "upstream_prompt_summary": upstream_prompt_summary,
                "image_keyword_categories": image_keyword_categories,
            },
        )
        if (
            source_prompt_summary.get("non_ascii_count", 0) > 0
            and upstream_prompt_summary.get("non_ascii_count", 0) == 0
        ):
            logger.warning(
                "Quick image non_ascii_lost_before_upstream",
                extra={
                    "model": model,
                    "source_prompt_summary": source_prompt_summary,
                    "upstream_prompt_summary": upstream_prompt_summary,
                    "image_keyword_categories": image_keyword_categories,
                },
            )

        # 上传附件
        file_ids: List[str] = []
        image_ids: List[str] = []
        if file_attachments or image_attachments:
            upload_service = UploadService()
            try:
                for attach_data in file_attachments:
                    file_id, _ = await upload_service.upload_file(attach_data, token)
                    file_ids.append(file_id)
                    logger.debug(f"Attachment uploaded: type=file, file_id={file_id}")
                for attach_data in image_attachments:
                    file_id, _ = await upload_service.upload_file(attach_data, token)
                    image_ids.append(file_id)
                    logger.debug(f"Attachment uploaded: type=image, file_id={file_id}")
            finally:
                await upload_service.close()

        all_attachments = file_ids + image_ids
        stream = stream if stream is not None else get_config("app.stream")

        model_config_override = {
            "temperature": temperature,
            "topP": top_p,
        }
        if reasoning_effort is not None:
            model_config_override["reasoningEffort"] = reasoning_effort

        response = await self.chat(
            token,
            message,
            grok_model,
            mode,
            stream,
            file_attachments=all_attachments,
            tool_overrides=None,
            model_config_override=model_config_override,
            use_mode_id=use_mode_id,
        )

        return response, stream, model


_AUTO_IMAGE_INTENT_PATTERNS = (
    re.compile(
        r"\b(generate|create|make|draw|paint|illustrate|render|design)\b.{0,24}\b(image|picture|photo|art|illustration|poster|avatar|wallpaper|logo)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(image|picture|photo|art|illustration|poster|avatar|wallpaper|logo)\b.{0,24}\b(of|for|showing|with)\b",
        re.IGNORECASE,
    ),
    # Short prompts with visual verbs — likely image generation.
    re.compile(
        r"^\s*(generate|create|draw|paint|illustrate|render|design)\b\s+(?!.*\b(code|report|list|file|text|summary|docs?|plan|test|function|class|table|query|email)\b).{2,60}$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(生图|出图|画图|绘图|生成图|生成图片|生成图像|做图|画一张|画个|来一张图|给我一张图|帮我生成)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(生成|制作|画|绘制|绘画|来|帮我).{0,10}(一张|一个|几张|些)?(图片|图像|照片|插画|海报|头像|壁纸|配图|表情包|图)",
        re.IGNORECASE,
    ),
    # Chinese: "生成/画 + 一个/一张 + <short object>" — visual generation intent.
    re.compile(
        r"(生成|画|绘制|帮我画|帮我生成|请生成|请画)\s*(一个|一张|一幅|个|张)\S",
        re.IGNORECASE,
    ),
)
_AUTO_IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*]\(([^)]+)\)")
_AUTO_IMAGE_BUFFER_LINE_LIMIT = 48
_AUTO_IMAGE_BUFFER_BYTE_LIMIT = 65536


@dataclass
class AutoImageStreamProbe:
    stream: AsyncGenerator[str, None]
    stream_completed: bool
    found_image_refs: bool
    has_streaming_image_event: bool


@dataclass
class QuickImagePreparedLine:
    raw_line: Any
    has_final_image: bool = False
    has_streaming_image_event: bool = False
    preview_urls: List[str] = field(default_factory=list)
    token_text: str = ""
    message_text: str = ""
    saw_model_response_message: bool = False
    has_tool_signal: bool = False
    response_id: str = ""


@dataclass
class QuickImagePendingState:
    prompt_intent: bool
    conversation_id: str = ""
    response_id: str = ""
    preview_urls: List[str] = field(default_factory=list)
    saw_final_image: bool = False
    saw_streaming_image_event: bool = False
    saw_model_response_message: bool = False
    token_text: str = ""
    message_text: str = ""
    saw_tool_call_result: bool = False

    def note_metadata(self, metadata: AppChatRequestMetadata) -> None:
        if not self.conversation_id:
            self.conversation_id = metadata.conversation_id or ""
        if not self.response_id:
            self.response_id = metadata.response_id or ""

    def note_prepared_line(self, prepared: QuickImagePreparedLine) -> None:
        self.saw_final_image = self.saw_final_image or prepared.has_final_image
        self.saw_streaming_image_event = (
            self.saw_streaming_image_event or prepared.has_streaming_image_event
        )
        self.saw_model_response_message = (
            self.saw_model_response_message or prepared.saw_model_response_message
        )
        self.saw_tool_call_result = (
            self.saw_tool_call_result or prepared.has_tool_signal
        )
        if prepared.token_text:
            self.token_text += prepared.token_text
        if prepared.saw_model_response_message:
            self.message_text = prepared.message_text
        if prepared.response_id and not self.response_id:
            self.response_id = prepared.response_id
        for preview_url in prepared.preview_urls:
            if preview_url not in self.preview_urls:
                self.preview_urls.append(preview_url)

    @property
    def candidate_urls(self) -> List[str]:
        return _derive_quick_image_candidate_urls(self.preview_urls)

    @property
    def effective_text(self) -> str:
        if self.message_text.strip():
            return self.message_text
        if self.token_text.strip():
            return self.token_text
        return self.message_text or self.token_text

    @property
    def stripped_text_length(self) -> int:
        return len(re.sub(r"\s+", "", self.effective_text or ""))

    @property
    def has_short_or_empty_text(self) -> bool:
        return (
            self.stripped_text_length <= _QUICK_IMAGE_STATE_WAIT_SHORT_TEXT_LIMIT
        )


def _extract_last_user_text(messages: List[Dict[str, Any]]) -> str:
    return _extract_last_user_text_for_summary(messages)


def _looks_like_auto_image_prompt(messages: List[Dict[str, Any]]) -> bool:
    text = _extract_last_user_text(messages)
    return _looks_like_image_prompt_text(text)


def _chat_result_has_rendered_image(result: Dict[str, Any]) -> bool:
    choices = result.get("choices") or []
    if not choices:
        return False
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return False
    return bool(_AUTO_IMAGE_MARKDOWN_RE.search(content)) or "/v1/files/image/" in content


def _should_probe_auto_image_limit(error: Exception) -> bool:
    if not isinstance(error, UpstreamException):
        return False
    details = error.details if isinstance(error.details, dict) else {}
    marker = details.get("error")
    code = details.get("error_code")
    return rate_limited(error) or marker in {"empty_result", "empty_stream"} or code == "blocked_no_final_image"


def _is_quick_mode_image_intent(
    model: str, messages: List[Dict[str, Any]]
) -> bool:
    return model in _QUICK_IMAGE_MODELS and _looks_like_auto_image_prompt(messages)


def _extract_chat_result_content(result: Dict[str, Any]) -> str:
    choices = result.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _log_quick_mode_result_anomaly(
    *,
    model: str,
    result: Dict[str, Any],
    metadata: AppChatRequestMetadata,
    prompt_summary: Dict[str, Any],
    image_keyword_categories: List[str],
) -> None:
    content = _extract_chat_result_content(result)
    event_name = (
        "Quick image empty_content_stop"
        if not content.strip()
        else "Quick image text_answer_without_image_events"
    )
    logger.warning(
        event_name,
        extra={
            "model": model,
            "conversation_id": metadata.conversation_id or "",
            "response_id": metadata.response_id or "",
            "prompt_summary": prompt_summary,
            "image_keyword_categories": image_keyword_categories,
            "content_summary": summarize_prompt_text(content),
        },
    )


def _is_final_image_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    return not _QUICK_IMAGE_PREVIEW_URL_RE.search(text)


def _quick_image_now() -> float:
    return time.monotonic()


_REMOVE_PREVIEW_URL = object()


def _quick_image_response_id(resp: Dict[str, Any]) -> str:
    if not isinstance(resp, dict):
        return ""
    raw_response_id = resp.get("responseId")
    if isinstance(raw_response_id, str) and raw_response_id.strip():
        return raw_response_id.strip()
    model_response = resp.get("modelResponse") or {}
    if not isinstance(model_response, dict):
        return ""
    model_response_id = model_response.get("responseId")
    if isinstance(model_response_id, str):
        return model_response_id.strip()
    return ""


def _has_quick_image_tool_signal(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _QUICK_IMAGE_TOOL_SIGNAL_KEYS:
                return True
            if _has_quick_image_tool_signal(item):
                return True
        return False
    if isinstance(value, list):
        return any(_has_quick_image_tool_signal(item) for item in value)
    if isinstance(value, str):
        return "<tool_call>" in value or "</tool_call>" in value
    return False


def _quick_image_state_wait_timeout(state: QuickImagePendingState = None) -> float:
    final_timeout = float(get_config("image.final_timeout") or 15)
    # When the stream contained image generation events, Grok is actively
    # generating — use the full timeout instead of the short state-wait cap.
    if state and state.saw_streaming_image_event:
        return max(0.1, final_timeout)
    return max(0.1, min(_QUICK_IMAGE_STATE_WAIT_MAX_SEC, final_timeout))


def _should_arm_quick_image_state_wait(state: QuickImagePendingState) -> bool:
    if state.prompt_intent or state.saw_final_image or state.saw_tool_call_result:
        return False
    if not state.conversation_id:
        return False
    # If we saw streaming image events, Grok is definitely generating an image.
    if state.saw_streaming_image_event:
        return True
    return state.has_short_or_empty_text


def _should_skip_quick_image_state_wait(state: QuickImagePendingState) -> bool:
    return bool(
        state.conversation_id
        and not state.prompt_intent
        and not state.saw_final_image
        and not state.saw_tool_call_result
        and not state.has_short_or_empty_text
    )


def _quick_image_log_extra(
    model: str,
    state: QuickImagePendingState,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "conversation_id": state.conversation_id or "",
        "response_id": state.response_id or "",
        "candidate_count": len(state.candidate_urls),
        "assistant_text_length": state.stripped_text_length,
        "has_streaming_image_event": state.saw_streaming_image_event,
        "has_preview_url": bool(state.preview_urls),
        "has_model_response_message": state.saw_model_response_message,
        "has_tool_call_result": state.saw_tool_call_result,
    }
    payload.update(extra)
    return payload


def _strip_preview_image_urls(value: Any) -> Any:
    if isinstance(value, dict):
        changed = False
        result: Dict[str, Any] = {}
        for key, item in value.items():
            stripped = _strip_preview_image_urls(item)
            if stripped is _REMOVE_PREVIEW_URL:
                changed = True
                continue
            if stripped is not item:
                changed = True
            result[key] = stripped
        return result if changed else value

    if isinstance(value, list):
        changed = False
        result: List[Any] = []
        for item in value:
            stripped = _strip_preview_image_urls(item)
            if stripped is _REMOVE_PREVIEW_URL:
                changed = True
                continue
            if stripped is not item:
                changed = True
            result.append(stripped)
        return result if changed else value

    if isinstance(value, str) and _QUICK_IMAGE_PREVIEW_URL_RE.search(value):
        return _REMOVE_PREVIEW_URL

    return value


def _collect_final_image_urls(value: Any) -> List[str]:
    seen: set[str] = set()
    results: List[str] = []

    for ref in proc_base._collect_image_references(value):
        if not _is_final_image_url(ref.url) or ref.url in seen:
            continue
        seen.add(ref.url)
        results.append(ref.url)

    if results:
        return results

    def _walk(item: Any) -> None:
        if isinstance(item, dict):
            for nested in item.values():
                _walk(nested)
            return
        if isinstance(item, list):
            for nested in item:
                _walk(nested)
            return
        if not isinstance(item, str):
            return
        text = item.strip()
        if not text or text in seen:
            return
        if "/generated/" not in text or "/image." not in text:
            return
        if not _is_final_image_url(text):
            return
        seen.add(text)
        results.append(text)

    _walk(value)
    return results


def _collect_preview_image_urls(value: Any) -> List[str]:
    seen: set[str] = set()
    results: List[str] = []

    def _add(url: str) -> None:
        text = str(url or "").strip()
        if not text or text in seen:
            return
        if not _QUICK_IMAGE_PREVIEW_URL_RE.search(text):
            return
        seen.add(text)
        results.append(text)

    for ref in proc_base._collect_image_references(value):
        _add(ref.url)

    def _walk(item: Any) -> None:
        if isinstance(item, dict):
            for nested in item.values():
                _walk(nested)
            return
        if isinstance(item, list):
            for nested in item:
                _walk(nested)
            return
        if not isinstance(item, str):
            return
        _add(item)

    _walk(value)
    return results


def _derive_final_image_url_from_preview(preview_url: str) -> str:
    text = str(preview_url or "").strip()
    if not text or not _QUICK_IMAGE_PREVIEW_URL_RE.search(text):
        return ""

    parts = urlsplit(text)
    path, changed = _QUICK_IMAGE_PREVIEW_SEGMENT_RE.subn("", parts.path)
    if changed <= 0 or not path:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _derive_quick_image_candidate_urls(preview_urls: List[str]) -> List[str]:
    seen: set[str] = set()
    candidates: List[str] = []
    for preview_url in preview_urls or []:
        candidate = _derive_final_image_url_from_preview(preview_url)
        if not candidate or candidate in seen or not _is_final_image_url(candidate):
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def _prepare_quick_image_raw_line(raw_line: Any) -> QuickImagePreparedLine:
    line = proc_base._normalize_line(raw_line)
    if not line:
        return QuickImagePreparedLine(raw_line=raw_line)

    try:
        payload = orjson.loads(line)
    except orjson.JSONDecodeError:
        return QuickImagePreparedLine(raw_line=raw_line)

    resp = payload.get("result", {}).get("response", {})
    preview_urls = _collect_preview_image_urls(resp)
    has_streaming_image_event = bool(resp.get("streamingImageGenerationResponse"))
    token_text = resp.get("token") if isinstance(resp.get("token"), str) else ""
    model_response = resp.get("modelResponse") or {}
    if not isinstance(model_response, dict):
        model_response = {}
    saw_model_response_message = "message" in model_response
    message_text = (
        model_response.get("message")
        if isinstance(model_response.get("message"), str)
        else ""
    )
    has_tool_signal = _has_quick_image_tool_signal(resp)
    response_id = _quick_image_response_id(resp)
    stripped_resp = _strip_preview_image_urls(resp)
    if stripped_resp is _REMOVE_PREVIEW_URL:
        stripped_resp = {}
    if stripped_resp is not resp:
        payload["result"]["response"] = stripped_resp
        raw_line = f"data: {orjson.dumps(payload).decode()}"
        resp = stripped_resp

    final_urls = _collect_final_image_urls(resp)
    return QuickImagePreparedLine(
        raw_line=raw_line,
        has_final_image=bool(final_urls),
        has_streaming_image_event=has_streaming_image_event,
        preview_urls=preview_urls,
        token_text=token_text,
        message_text=message_text,
        saw_model_response_message=saw_model_response_message,
        has_tool_signal=has_tool_signal,
        response_id=response_id,
    )


def _build_quick_image_completion_line(
    final_urls: List[str], response_id: str = ""
) -> str:
    payload: Dict[str, Any] = {
        "result": {
            "response": {
                "streamingImageGenerationResponse": {
                    "final": [{"imageUrl": url} for url in final_urls]
                }
            }
        }
    }
    if response_id:
        payload["result"]["response"]["responseId"] = response_id
    return f"data: {orjson.dumps(payload).decode()}"


async def _poll_quick_image_final_urls(
    *,
    token: str,
    conversation_id: str,
    model: str,
    preview_urls: List[str] | None = None,
    response_id: str = "",
    timeout: float | None = None,
) -> List[str]:
    wait_timeout = (
        float(timeout)
        if timeout is not None
        else float(get_config("image.final_timeout") or 15)
    )
    deadline = _quick_image_now() + max(0.1, wait_timeout)
    browser = get_config("proxy.browser")
    known_preview_urls = list(preview_urls or [])
    saw_candidates = False
    logged_candidate_urls: tuple[str, ...] = ()

    async def _probe_asset_candidates(
        session: ResettableSession,
        candidate_urls: List[str],
    ) -> List[str]:
        ready_urls: List[str] = []
        for candidate_url in candidate_urls:
            try:
                if await AppAssetReverse.probe(session, candidate_url):
                    ready_urls.append(candidate_url)
            except Exception as exc:
                logger.warning(
                    "Quick image asset probe failed",
                    extra={
                        "model": model,
                        "conversation_id": conversation_id or "",
                        "response_id": response_id,
                        "candidate_url": candidate_url,
                        "error": str(exc),
                    },
                )
        if ready_urls:
            logger.info(
                "Quick image asset probe succeeded",
                extra={
                    "model": model,
                    "conversation_id": conversation_id or "",
                    "response_id": response_id,
                    "candidate_count": len(ready_urls),
                },
            )
        return ready_urls

    async with ResettableSession(impersonate=browser) as session:
        while _quick_image_now() < deadline:
            if conversation_id:
                try:
                    payload = await AppChatConversationReverse.request(
                        session,
                        token,
                        conversation_id,
                    )
                    poll_previews = _collect_preview_image_urls(payload)
                    for preview_url in poll_previews:
                        if preview_url not in known_preview_urls:
                            known_preview_urls.append(preview_url)
                    final_urls = _collect_final_image_urls(payload)
                    logger.info(
                        "Quick image poll iteration",
                        extra={
                            "model": model,
                            "conversation_id": conversation_id,
                            "poll_previews": poll_previews[:3],
                            "final_urls": final_urls[:3],
                            "known_previews_total": len(known_preview_urls),
                            "payload_type": type(payload).__name__,
                            "payload_keys": sorted(payload.keys())[:10] if isinstance(payload, dict) else [],
                        },
                    )
                    if final_urls:
                        return final_urls
                except Exception as exc:
                    logger.warning(
                        "Quick image conversations_v2 poll failed",
                        extra={
                            "model": model,
                            "conversation_id": conversation_id,
                            "response_id": response_id,
                            "error": str(exc),
                        },
                    )

            candidate_urls = _derive_quick_image_candidate_urls(known_preview_urls)
            if candidate_urls:
                saw_candidates = True
                candidate_key = tuple(candidate_urls)
                if candidate_key != logged_candidate_urls:
                    logged_candidate_urls = candidate_key
                    logger.info(
                        "Quick image asset candidates derived",
                        extra={
                            "model": model,
                            "conversation_id": conversation_id or "",
                            "response_id": response_id,
                            "candidate_count": len(candidate_urls),
                        },
                    )
                ready_urls = await _probe_asset_candidates(session, candidate_urls)
                if ready_urls:
                    return ready_urls
            elif not conversation_id:
                logger.info(
                    "Quick image asset fallback has no candidates",
                    extra={
                        "model": model,
                        "conversation_id": "",
                        "response_id": response_id,
                        "candidate_count": 0,
                    },
                )
                break

            remaining = deadline - _quick_image_now()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_QUICK_IMAGE_POLL_INTERVAL_SEC, remaining))

    if saw_candidates:
        logger.warning(
            "Quick image asset probe timed out",
            extra={
                "model": model,
                "conversation_id": conversation_id or "",
                "response_id": response_id,
                "candidate_count": len(logged_candidate_urls),
            },
        )

    return []


async def _augment_quick_image_response_stream(
    request_result: AppChatRequestResult,
    *,
    token: str,
    model: str,
    prompt_intent: bool,
) -> AsyncGenerator[str, None]:
    metadata = getattr(request_result, "metadata", AppChatRequestMetadata())
    state = QuickImagePendingState(prompt_intent=prompt_intent)
    state.note_metadata(metadata)

    logger.info(
        "Quick image wait started",
        extra=_quick_image_log_extra(model, state),
    )

    async for raw_line in request_result:
        prepared_line = _prepare_quick_image_raw_line(raw_line)
        new_preview_urls = [
            preview_url
            for preview_url in prepared_line.preview_urls
            if preview_url not in state.preview_urls
        ]
        state.note_prepared_line(prepared_line)
        if new_preview_urls:
            logger.info(
                "Quick image preview detected",
                extra=_quick_image_log_extra(model, state),
            )
        yield prepared_line.raw_line

    # Re-read metadata after stream completes: conversation_id is populated
    # incrementally by _update_metadata_from_line during iteration.
    state.note_metadata(metadata)
    logger.info(
        "Quick image post-stream state",
        extra={
            "model": model,
            "conversation_id": state.conversation_id or "",
            "preview_urls": state.preview_urls[:5],
            "saw_streaming_image_event": state.saw_streaming_image_event,
            "saw_final_image": state.saw_final_image,
            "prompt_intent": state.prompt_intent,
            "text_len": state.stripped_text_length,
        },
    )

    if state.saw_final_image:
        logger.info(
            "Quick image completed in primary stream",
            extra=_quick_image_log_extra(model, state),
        )
        return

    if state.prompt_intent and not state.saw_streaming_image_event and not state.preview_urls:
        logger.warning(
            "Quick image text_answer_without_image_events",
            extra=_quick_image_log_extra(model, state),
        )

    wait_timeout: float | None = None
    wait_mode = ""

    if state.prompt_intent:
        if not state.conversation_id and not state.preview_urls:
            logger.warning(
                "Quick image wait missing conversation id; degrading to text",
                extra=_quick_image_log_extra(model, state),
            )
            return
        wait_mode = "prompt"
        wait_timeout = float(get_config("image.final_timeout") or 15)
        logger.info(
            "Quick image wait_armed_by_prompt",
            extra=_quick_image_log_extra(
                model,
                state,
                wait_budget_sec=wait_timeout,
            ),
        )
    elif _should_arm_quick_image_state_wait(state):
        wait_mode = "state"
        wait_timeout = _quick_image_state_wait_timeout(state)
        logger.info(
            "Quick image wait_armed_by_state",
            extra=_quick_image_log_extra(
                model,
                state,
                wait_budget_sec=wait_timeout,
            ),
        )
    elif _should_skip_quick_image_state_wait(state):
        logger.info(
            "Quick image state_wait_skipped_long_text",
            extra=_quick_image_log_extra(model, state),
        )
        return
    else:
        return

    if wait_mode == "prompt" and state.conversation_id:
        logger.info(
            "Quick image entering conversations_v2 wait",
            extra=_quick_image_log_extra(model, state),
        )
    elif wait_mode == "prompt":
        logger.info(
            "Quick image wait missing conversation id; using asset fallback",
            extra=_quick_image_log_extra(model, state),
        )

    final_urls = await _poll_quick_image_final_urls(
        token=token,
        conversation_id=state.conversation_id,
        model=model,
        preview_urls=state.preview_urls,
        response_id=state.response_id,
        timeout=wait_timeout,
    )
    if not final_urls:
        if wait_mode == "state":
            logger.info(
                "Quick image state_wait_timeout",
                extra=_quick_image_log_extra(
                    model,
                    state,
                    wait_budget_sec=wait_timeout or 0,
                ),
            )
        else:
            logger.warning(
                "Quick image wait timed out; degrading to text",
                extra=_quick_image_log_extra(
                    model,
                    state,
                    wait_budget_sec=wait_timeout or 0,
                ),
            )
        return

    if state.effective_text.strip():
        logger.info(
            "Quick image recovered_after_text",
            extra=_quick_image_log_extra(
                model,
                state,
                wait_mode=wait_mode,
                final_image_count=len(final_urls),
            ),
        )
    else:
        logger.info(
            "Quick image empty_stop_recovered",
            extra=_quick_image_log_extra(
                model,
                state,
                wait_mode=wait_mode,
                final_image_count=len(final_urls),
            ),
        )

    logger.info(
        "Quick image wait completed",
        extra=_quick_image_log_extra(
            model,
            state,
            wait_mode=wait_mode,
            final_image_count=len(final_urls),
        ),
    )
    yield _build_quick_image_completion_line(
        final_urls,
        response_id=state.response_id,
    )


async def _consume_chat_usage(token_mgr, token: str, model: str) -> None:
    try:
        model_info = ModelService.get(model)
        effort = (
            EffortType.HIGH
            if (model_info and model_info.cost.value == "high")
            else EffortType.LOW
        )
        await token_mgr.consume(token, effort)
        logger.info(f"Chat completed: model={model}, effort={effort.value}")
    except Exception as e:
        logger.warning(f"Failed to record usage: {e}")


def _iter_raw_stream(
    prefetched: List[bytes], iterator: Any
) -> AsyncGenerator[bytes, None]:
    async def _gen() -> AsyncGenerator[bytes, None]:
        for item in prefetched:
            yield item
        async for item in iterator:
            yield item

    return _gen()


def _inspect_stream_line(raw_line: Any) -> tuple[bool, bool]:
    line = proc_base._normalize_line(raw_line)
    if not line:
        return False, False
    try:
        data = orjson.loads(line)
    except orjson.JSONDecodeError:
        return False, False
    resp = data.get("result", {}).get("response", {})
    has_streaming_image_event = bool(resp.get("streamingImageGenerationResponse"))
    has_refs = bool(proc_base._collect_image_references(resp))
    return has_refs, has_streaming_image_event


async def _probe_auto_image_stream(
    response: AsyncIterable[bytes],
    *,
    model_name: str,
    token: str,
    show_think: bool,
    tools: List[Dict[str, Any]] = None,
    tool_choice: Any = None,
) -> AutoImageStreamProbe:
    iterator = response.__aiter__()
    prefetched: list[bytes] = []
    prefetched_bytes = 0
    found_image_refs = False
    has_streaming_image_event = False
    stream_completed = False

    while True:
        try:
            raw_line = await iterator.__anext__()
        except StopAsyncIteration:
            stream_completed = True
            break

        prefetched.append(raw_line)
        if isinstance(raw_line, (bytes, bytearray)):
            prefetched_bytes += len(raw_line)
        else:
            prefetched_bytes += len(str(raw_line).encode("utf-8", errors="ignore"))

        line_has_refs, line_has_streaming_event = _inspect_stream_line(raw_line)
        found_image_refs = found_image_refs or line_has_refs
        has_streaming_image_event = has_streaming_image_event or line_has_streaming_event

        if found_image_refs:
            break
        if (
            not has_streaming_image_event
            and (
                len(prefetched) >= _AUTO_IMAGE_BUFFER_LINE_LIMIT
                or prefetched_bytes >= _AUTO_IMAGE_BUFFER_BYTE_LIMIT
            )
        ):
            break

    processor = StreamProcessor(
        model_name,
        token,
        show_think,
        tools=tools,
        tool_choice=tool_choice,
    )
    return AutoImageStreamProbe(
        stream=processor.process(_iter_raw_stream(prefetched, iterator)),
        stream_completed=stream_completed,
        found_image_refs=found_image_refs,
        has_streaming_image_event=has_streaming_image_event,
    )


class ChatService:
    """Chat 业务服务"""

    @staticmethod
    async def completions(
        model: str,
        messages: List[Dict[str, Any]],
        stream: bool = None,
        reasoning_effort: str | None = None,
        temperature: float = 0.8,
        top_p: float = 0.95,
        tools: List[Dict[str, Any]] = None,
        tool_choice: Any = None,
        parallel_tool_calls: bool = True,
    ):
        """Chat Completions 入口"""
        # 获取 token
        token_mgr = await get_token_manager()
        await token_mgr.reload_if_stale()

        if model_requires_special_subscription(model):
            if not token_mgr.has_entitled_token_for_model(model):
                reason = token_mgr.model_access_denial_reason(model)
                logger.warning(
                    "Model access denied: model={} reason={}",
                    model,
                    reason or "insufficient_heavy_subscription",
                )
                raise model_access_denied_error(model)

        # 解析参数
        if reasoning_effort is None:
            show_think = get_config("app.thinking")
        else:
            show_think = reasoning_effort != "none"
        is_stream = stream if stream is not None else get_config("app.stream")
        last_user_text = _extract_last_user_text(messages)
        prompt_summary = summarize_prompt_text(last_user_text)
        image_keyword_categories = _detect_image_keyword_categories(last_user_text)
        quick_mode_image_intent = _is_quick_mode_image_intent(model, messages)
        if model in _QUICK_IMAGE_MODELS or prompt_summary.get("has_image_keywords"):
            logger.info(
                "Quick image intent evaluated",
                extra={
                    "model": model,
                    "quick_image_intent": quick_mode_image_intent,
                    "prompt_summary": prompt_summary,
                    "image_keyword_categories": image_keyword_categories,
                    "stream": bool(is_stream),
                },
            )
        quota_requirement = (
            image_quota_requirement() if quick_mode_image_intent else None
        )

        # 跨 Token 重试循环
        tried_tokens: set[str] = set()
        exhausted_tokens: set[str] = set()
        max_token_retries = int(get_config("retry.max_retry") or 3)
        last_error: Exception | None = None

        def _raise_no_token(total_candidates: int = 0) -> None:
            if quota_requirement and all_candidate_tokens_exhausted(
                total_candidates, exhausted_tokens
            ):
                raise image_limit_exception(total_candidates)
            if last_error:
                raise last_error
            raise AppException(
                message="No available tokens. Please try again later.",
                error_type=ErrorType.RATE_LIMIT.value,
                code="rate_limit_exceeded",
                status_code=429,
            )

        for attempt in range(max_token_retries):
            # 选择 token
            selection_total = 0
            if quota_requirement:
                selection = await select_token_for_requirement(
                    token_mgr,
                    model,
                    tried=tried_tokens,
                    requirement=quota_requirement,
                    exhausted_tokens=exhausted_tokens,
                )
                token = selection.token
                selection_total = selection.total_candidates
            else:
                token = await pick_token(token_mgr, model, tried_tokens)
            if not token:
                _raise_no_token(selection_total)

            tried_tokens.add(token)

            try:
                # 请求 Grok
                service = GrokChatService()
                response, _, model_name = await service.chat_openai(
                    token,
                    model,
                    messages,
                    stream=is_stream,
                    reasoning_effort=reasoning_effort,
                    temperature=temperature,
                    top_p=top_p,
                    tools=tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                )

                if not isinstance(response, AppChatRequestResult):
                    response = AppChatRequestResult(
                        stream=response,
                        metadata=AppChatRequestMetadata(),
                    )

                raw_response: AsyncIterable[Any] = response
                if model in _QUICK_IMAGE_MODELS:
                    raw_response = _augment_quick_image_response_stream(
                        response,
                        token=token,
                        model=model,
                        prompt_intent=quick_mode_image_intent,
                    )

                # 处理响应
                if is_stream:
                    logger.debug(f"Processing stream response: model={model}")
                    processor = StreamProcessor(
                        model_name,
                        token,
                        show_think,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                    return wrap_stream_with_usage(
                        processor.process(raw_response), token_mgr, token, model
                    )

                # 非流式
                logger.debug(f"Processing non-stream response: model={model}")
                result = await CollectProcessor(
                    model_name,
                    token,
                    tools=tools,
                    tool_choice=tool_choice,
                ).process(raw_response)
                if quick_mode_image_intent and not _chat_result_has_rendered_image(result):
                    _log_quick_mode_result_anomaly(
                        model=model,
                        result=result,
                        metadata=response.metadata,
                        prompt_summary=prompt_summary,
                        image_keyword_categories=image_keyword_categories,
                    )
                await _consume_chat_usage(token_mgr, token, model)
                return result

            except UpstreamException as e:
                last_error = e

                if quota_requirement and _should_probe_auto_image_limit(e):
                    if await confirm_quota_exhausted(
                        token_mgr,
                        token,
                        quota_requirement,
                        exhausted_tokens,
                    ):
                        if all_candidate_tokens_exhausted(
                            selection_total, exhausted_tokens
                        ):
                            raise image_limit_exception(selection_total)
                        logger.warning(
                            "Quick image request hit quota limit, trying next token",
                            extra={
                                "model": model,
                                "attempt": attempt + 1,
                                "token": f"{token[:10]}...",
                            },
                        )
                        continue

                if rate_limited(e):
                    resolution = await resolve_rate_limit_hit(
                        token_mgr,
                        token,
                        model,
                        requirement=quota_requirement,
                        exhausted_tokens=exhausted_tokens,
                    )
                    if (
                        quota_requirement
                        and resolution.action == RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED
                        and all_candidate_tokens_exhausted(
                            selection_total, exhausted_tokens
                        )
                    ):
                        raise image_limit_exception(selection_total)
                    if resolution.action == RATE_LIMIT_ACTION_RETRY_SAME_TOKEN:
                        tried_tokens.discard(token)
                        if resolution.retry_after_seconds > 0:
                            await asyncio.sleep(resolution.retry_after_seconds)
                        logger.info(
                            f"Token {token[:10]}... rate limit cleared by probe, "
                            f"retrying same token (attempt {attempt + 1}/{max_token_retries})"
                        )
                    else:
                        logger.warning(
                            f"Token {token[:10]}... rate limited (429), "
                            f"trying next token (attempt {attempt + 1}/{max_token_retries})"
                        )
                    continue

                if transient_upstream(e):
                    has_alternative_token = token_mgr.has_available_token_for_model(
                        model,
                        exclude=tried_tokens | exhausted_tokens,
                    )
                    if not has_alternative_token:
                        raise
                    logger.warning(
                        f"Transient upstream error for token {token[:10]}..., "
                        f"trying next token (attempt {attempt + 1}/{max_token_retries}): {e}"
                    )
                    continue

                # 非 429 错误，不换 token，直接抛出
                raise

        # 所有 token 都 429，抛出最后的错误
        if last_error:
            raise last_error
        raise AppException(
            message="No available tokens. Please try again later.",
            error_type=ErrorType.RATE_LIMIT.value,
            code="rate_limit_exceeded",
            status_code=429,
        )


def _image_id_from_url(url: str) -> str:
    parts = [part for part in (url or "").split("/") if part]
    if len(parts) >= 2 and "." in parts[-1]:
        return parts[-2]
    if parts:
        return parts[-1]
    return "image"


def _image_log_extra(
    model: str,
    refs: List[proc_base.ImageReference],
    *,
    has_streaming_image_event: bool,
) -> dict[str, Any]:
    return {
        "model": model,
        "source_shape": ",".join(sorted({ref.source_shape for ref in refs})),
        "image_ref_count": len(refs),
        "has_streaming_image_event": has_streaming_image_event,
    }


async def _render_chat_image_refs(
    processor: proc_base.BaseProcessor,
    refs: List[proc_base.ImageReference],
    seen_urls: set[str],
    *,
    has_streaming_image_event: bool,
) -> list[str]:
    if not refs:
        return []

    logger.debug(
        "Chat parsed upstream images",
        extra=_image_log_extra(
            processor.model,
            refs,
            has_streaming_image_event=has_streaming_image_event,
        ),
    )

    outputs: list[str] = []
    dl_service = processor._get_dl()
    for ref in refs:
        if ref.url in seen_urls:
            continue
        seen_urls.add(ref.url)
        rendered = await dl_service.render_image(
            ref.url,
            processor.token,
            _image_id_from_url(ref.url),
        )
        if rendered:
            outputs.append(rendered)
    return outputs


class StreamProcessor(proc_base.BaseProcessor):
    """Stream response processor."""

    def __init__(self, model: str, token: str = "", show_think: bool = None, tools: List[Dict[str, Any]] = None, tool_choice: Any = None):
        super().__init__(model, token)
        self.response_id: str = None
        self.fingerprint: str = ""
        self.rollout_id: str = ""
        self.think_opened: bool = False
        self.think_closed_once: bool = False
        self.image_think_active: bool = False
        self.role_sent: bool = False
        self.filter_tags = get_config("app.filter_tags")
        self.tool_usage_enabled = (
            "xai:tool_usage_card" in (self.filter_tags or [])
        )
        self._tool_usage_opened = False
        self._tool_usage_buffer = ""

        self.show_think = bool(show_think)
        self.tools = tools
        self.tool_choice = tool_choice
        self._tool_stream_enabled = bool(tools) and tool_choice != "none"
        self._tool_state = "text"
        self._tool_buffer = ""
        self._tool_partial = ""
        self._tool_calls_seen = False
        self._tool_call_index = 0
        self._seen_image_urls: set[str] = set()

    def _with_tool_index(self, tool_call: Any) -> Any:
        if not isinstance(tool_call, dict):
            return tool_call
        if tool_call.get("index") is None:
            tool_call = dict(tool_call)
            tool_call["index"] = self._tool_call_index
            self._tool_call_index += 1
        return tool_call

    def _filter_tool_card(self, token: str) -> str:
        if not token or not self.tool_usage_enabled:
            return token

        output_parts: list[str] = []
        rest = token
        start_tag = "<xai:tool_usage_card"
        end_tag = "</xai:tool_usage_card>"

        while rest:
            if self._tool_usage_opened:
                end_idx = rest.find(end_tag)
                if end_idx == -1:
                    self._tool_usage_buffer += rest
                    return "".join(output_parts)
                end_pos = end_idx + len(end_tag)
                self._tool_usage_buffer += rest[:end_pos]
                line = extract_tool_text(self._tool_usage_buffer, self.rollout_id)
                if line:
                    if output_parts and not output_parts[-1].endswith("\n"):
                        output_parts[-1] += "\n"
                    output_parts.append(f"{line}\n")
                self._tool_usage_buffer = ""
                self._tool_usage_opened = False
                rest = rest[end_pos:]
                continue

            start_idx = rest.find(start_tag)
            if start_idx == -1:
                output_parts.append(rest)
                break

            if start_idx > 0:
                output_parts.append(rest[:start_idx])

            end_idx = rest.find(end_tag, start_idx)
            if end_idx == -1:
                self._tool_usage_opened = True
                self._tool_usage_buffer = rest[start_idx:]
                break

            end_pos = end_idx + len(end_tag)
            raw_card = rest[start_idx:end_pos]
            line = extract_tool_text(raw_card, self.rollout_id)
            if line:
                if output_parts and not output_parts[-1].endswith("\n"):
                    output_parts[-1] += "\n"
                output_parts.append(f"{line}\n")
            rest = rest[end_pos:]

        return "".join(output_parts)

    def _filter_token(self, token: str) -> str:
        """Filter special tags in current token only."""
        if not token:
            return token

        if self.tool_usage_enabled:
            token = self._filter_tool_card(token)
            if not token:
                return ""

        if not self.filter_tags:
            return token

        for tag in self.filter_tags:
            if tag == "xai:tool_usage_card":
                continue
            if f"<{tag}" in token or f"</{tag}" in token:
                return ""

        return token

    def _suffix_prefix(self, text: str, tag: str) -> int:
        if not text or not tag:
            return 0
        max_keep = min(len(text), len(tag) - 1)
        for keep in range(max_keep, 0, -1):
            if text.endswith(tag[:keep]):
                return keep
        return 0

    def _handle_tool_stream(self, chunk: str) -> list[tuple[str, Any]]:
        events: list[tuple[str, Any]] = []
        if not chunk:
            return events

        start_tag = "<tool_call>"
        end_tag = "</tool_call>"
        data = f"{self._tool_partial}{chunk}"
        self._tool_partial = ""

        while data:
            if self._tool_state == "text":
                start_idx = data.find(start_tag)
                if start_idx == -1:
                    keep = self._suffix_prefix(data, start_tag)
                    emit = data[:-keep] if keep else data
                    if emit:
                        events.append(("text", emit))
                    self._tool_partial = data[-keep:] if keep else ""
                    break

                before = data[:start_idx]
                if before:
                    events.append(("text", before))
                data = data[start_idx + len(start_tag) :]
                self._tool_state = "tool"
                continue

            end_idx = data.find(end_tag)
            if end_idx == -1:
                keep = self._suffix_prefix(data, end_tag)
                append = data[:-keep] if keep else data
                if append:
                    self._tool_buffer += append
                self._tool_partial = data[-keep:] if keep else ""
                break

            self._tool_buffer += data[:end_idx]
            data = data[end_idx + len(end_tag) :]
            tool_call = parse_tool_call_block(self._tool_buffer, self.tools)
            if tool_call:
                events.append(("tool", self._with_tool_index(tool_call)))
                self._tool_calls_seen = True
            self._tool_buffer = ""
            self._tool_state = "text"

        return events

    def _flush_tool_stream(self) -> list[tuple[str, Any]]:
        events: list[tuple[str, Any]] = []
        if self._tool_state == "text":
            if self._tool_partial:
                events.append(("text", self._tool_partial))
                self._tool_partial = ""
            return events

        raw = f"{self._tool_buffer}{self._tool_partial}"
        tool_call = parse_tool_call_block(raw, self.tools)
        if tool_call:
            events.append(("tool", self._with_tool_index(tool_call)))
            self._tool_calls_seen = True
        elif raw:
            events.append(("text", f"<tool_call>{raw}"))
        self._tool_buffer = ""
        self._tool_partial = ""
        self._tool_state = "text"
        return events

    def _sse(self, content: str = "", role: str = None, finish: str = None, tool_calls: list = None) -> str:
        """Build SSE response."""
        delta = {}
        if role:
            delta["role"] = role
            delta["content"] = ""
        elif tool_calls is not None:
            delta["tool_calls"] = tool_calls
        elif content:
            delta["content"] = content

        chunk = {
            "id": self.response_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": self.fingerprint,
            "choices": [
                {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
            ],
        }
        return f"data: {orjson.dumps(chunk).decode()}\n\n"

    def _close_think_chunk(self) -> Optional[str]:
        if not self.think_opened:
            return None
        self.think_opened = False
        self.think_closed_once = True
        return self._sse("\n</think>\n")

    async def process(self, response: AsyncIterable[bytes]) -> AsyncGenerator[str, None]:
        """Process stream response.

        Args:
            response: AsyncIterable[bytes], async iterable of bytes

        Returns:
            AsyncGenerator[str, None], async generator of strings
        """
        idle_timeout = get_config("chat.stream_timeout")

        try:
            async for line in proc_base._with_idle_timeout(
                response, idle_timeout, self.model
            ):
                line = proc_base._normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})
                is_thinking = bool(resp.get("isThinking"))
                # isThinking controls <think> tagging
                # when absent, treat as False

                if (llm := resp.get("llmInfo")) and not self.fingerprint:
                    self.fingerprint = llm.get("modelHash", "")
                if rid := resp.get("responseId"):
                    self.response_id = rid
                if rid := resp.get("rolloutId"):
                    self.rollout_id = str(rid)

                if not self.role_sent:
                    yield self._sse(role="assistant")
                    self.role_sent = True

                # Surface stream-level errors (e.g. image generation rate limit)
                if err := resp.get("error"):
                    err_msg = err.get("message", "") if isinstance(err, dict) else str(err)
                    if err_msg:
                        close_chunk = self._close_think_chunk()
                        if close_chunk:
                            yield close_chunk
                        yield self._sse(err_msg)
                    continue

                if img := resp.get("streamingImageGenerationResponse"):
                    refs = proc_base._collect_image_references(
                        {"streamingImageGenerationResponse": img}
                    )
                    if refs:
                        close_chunk = self._close_think_chunk()
                        if close_chunk:
                            yield close_chunk
                        self.image_think_active = False
                        for rendered in await _render_chat_image_refs(
                            self,
                            refs,
                            self._seen_image_urls,
                            has_streaming_image_event=True,
                        ):
                            yield self._sse(f"{rendered}\n")
                        continue
                    elif not self.show_think:
                        logger.warning(
                            "Chat stream saw streaming image event without image refs",
                            extra={
                                "model": self.model,
                                "source_shape": "",
                                "image_ref_count": 0,
                                "has_streaming_image_event": True,
                            },
                        )
                        self.image_think_active = False
                        continue
                    else:
                        self.image_think_active = True
                    if not self.think_opened:
                        yield self._sse("<think>\n")
                        self.think_opened = True
                    idx = img.get("imageIndex", 0) + 1
                    progress = img.get("progress", 0)
                    yield self._sse(
                        f"正在生成第{idx}张图片中，当前进度{progress}%\n"
                    )
                    continue

                if mr := resp.get("modelResponse"):
                    close_chunk = self._close_think_chunk()
                    if close_chunk:
                        yield close_chunk
                    self.image_think_active = False
                    for rendered in await _render_chat_image_refs(
                        self,
                        proc_base._collect_image_references(mr),
                        self._seen_image_urls,
                        has_streaming_image_event=False,
                    ):
                        yield self._sse(f"{rendered}\n")

                    if (
                        (meta := mr.get("metadata", {}))
                        .get("llm_info", {})
                        .get("modelHash")
                    ):
                        self.fingerprint = meta["llm_info"]["modelHash"]
                    continue

                if card := resp.get("cardAttachment"):
                    json_data = card.get("jsonData")
                    if isinstance(json_data, str) and json_data.strip():
                        try:
                            card_data = orjson.loads(json_data)
                        except orjson.JSONDecodeError:
                            card_data = None
                        if isinstance(card_data, dict):
                            image = card_data.get("image") or {}
                            original = image.get("original")
                            title = image.get("title") or ""
                            if original and original not in self._seen_image_urls:
                                self._seen_image_urls.add(original)
                                title_safe = title.replace("\n", " ").strip()
                                if title_safe:
                                    yield self._sse(f"![{title_safe}]({original})\n")
                                else:
                                    yield self._sse(f"![image]({original})\n")
                    continue

                if (token := resp.get("token")) is not None:
                    if not token:
                        continue
                    if is_thinking and self.think_closed_once and not self.image_think_active:
                        continue
                    filtered = self._filter_token(token)
                    if not filtered:
                        continue
                    in_think = (
                        (is_thinking and not self.think_closed_once)
                        or self.image_think_active
                    )
                    if in_think:
                        if not self.show_think:
                            continue
                        if not self.think_opened:
                            yield self._sse("<think>\n")
                            self.think_opened = True
                    else:
                        if self.think_opened:
                            yield self._sse("\n</think>\n")
                            self.think_opened = False
                            self.think_closed_once = True

                    if in_think:
                        yield self._sse(filtered)
                        continue

                    if self._tool_stream_enabled:
                        for kind, payload in self._handle_tool_stream(filtered):
                            if kind == "text":
                                yield self._sse(payload)
                            elif kind == "tool":
                                yield self._sse(tool_calls=[payload])
                        continue

                    yield self._sse(filtered)

            if self.think_opened:
                yield self._sse("</think>\n")
                self.think_closed_once = True

            if self._tool_stream_enabled:
                for kind, payload in self._flush_tool_stream():
                    if kind == "text":
                        yield self._sse(payload)
                    elif kind == "tool":
                        yield self._sse(tool_calls=[payload])
                finish_reason = "tool_calls" if self._tool_calls_seen else "stop"
                yield self._sse(finish=finish_reason)
            else:
                yield self._sse(finish="stop")

            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.debug("Stream cancelled by client", extra={"model": self.model})
        except StreamIdleTimeoutError as e:
            raise UpstreamException(
                message=f"Stream idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                },
            )
        except RequestsError as e:
            if proc_base._is_http2_error(e):
                logger.warning(f"HTTP/2 stream error: {e}", extra={"model": self.model})
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"error": str(e), "type": "http2_stream_error"},
                )
            logger.error(f"Stream request error: {e}", extra={"model": self.model})
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(
                f"Stream processing error: {e}",
                extra={"model": self.model, "error_type": type(e).__name__},
            )
            raise
        finally:
            await self.close()


class CollectProcessor(proc_base.BaseProcessor):
    """Non-stream response processor."""

    def __init__(self, model: str, token: str = "", tools: List[Dict[str, Any]] = None, tool_choice: Any = None):
        super().__init__(model, token)
        self.filter_tags = get_config("app.filter_tags")
        self.tools = tools
        self.tool_choice = tool_choice

    def _filter_content(self, content: str) -> str:
        """Filter special tags in content."""
        if not content or not self.filter_tags:
            return content

        result = content
        if "xai:tool_usage_card" in self.filter_tags:
            rollout_id = ""
            rollout_match = re.search(
                r"<rolloutId>(.*?)</rolloutId>", result, flags=re.DOTALL
            )
            if rollout_match:
                rollout_id = rollout_match.group(1).strip()

            result = re.sub(
                r"<xai:tool_usage_card[^>]*>.*?</xai:tool_usage_card>",
                lambda match: (
                    f"{extract_tool_text(match.group(0), rollout_id)}\n"
                    if extract_tool_text(match.group(0), rollout_id)
                    else ""
                ),
                result,
                flags=re.DOTALL,
            )

        for tag in self.filter_tags:
            if tag == "xai:tool_usage_card":
                continue
            pattern = rf"<{re.escape(tag)}[^>]*>.*?</{re.escape(tag)}>|<{re.escape(tag)}[^>]*/>"
            result = re.sub(pattern, "", result, flags=re.DOTALL)

        return result

    async def process(self, response: AsyncIterable[bytes]) -> dict[str, Any]:
        """Process and collect full response."""
        response_id = ""
        fingerprint = ""
        content = ""
        streaming_outputs: list[str] = []
        seen_image_urls: set[str] = set()
        idle_timeout = get_config("chat.stream_timeout")

        try:
            async for line in proc_base._with_idle_timeout(
                response, idle_timeout, self.model
            ):
                line = proc_base._normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                if (llm := resp.get("llmInfo")) and not fingerprint:
                    fingerprint = llm.get("modelHash", "")

                # Surface stream-level errors (e.g. image generation rate limit)
                if err := resp.get("error"):
                    err_msg = err.get("message", "") if isinstance(err, dict) else str(err)
                    if err_msg and not content:
                        content = err_msg

                if img := resp.get("streamingImageGenerationResponse"):
                    refs = proc_base._collect_image_references(
                        {"streamingImageGenerationResponse": img}
                    )
                    if refs:
                        streaming_outputs.extend(
                            await _render_chat_image_refs(
                                self,
                                refs,
                                seen_image_urls,
                                has_streaming_image_event=True,
                            )
                        )
                    else:
                        logger.warning(
                            "Chat collect saw streaming image event without image refs",
                            extra={
                                "model": self.model,
                                "source_shape": "",
                                "image_ref_count": 0,
                                "has_streaming_image_event": True,
                            },
                        )

                if mr := resp.get("modelResponse"):
                    response_id = mr.get("responseId", "")
                    mr_message = mr.get("message", "")
                    # Preserve stream error content when modelResponse.message is empty
                    if mr_message or not content:
                        content = mr_message

                    card_map: dict[str, tuple[str, str]] = {}
                    for raw in mr.get("cardAttachmentsJson") or []:
                        if not isinstance(raw, str) or not raw.strip():
                            continue
                        try:
                            card_data = orjson.loads(raw)
                        except orjson.JSONDecodeError:
                            continue
                        if not isinstance(card_data, dict):
                            continue
                        card_id = card_data.get("id")
                        image = card_data.get("image") or {}
                        original = image.get("original")
                        if not card_id or not original:
                            continue
                        title = image.get("title") or ""
                        card_map[card_id] = (title, original)

                    if content and card_map:
                        def _render_card(match: re.Match) -> str:
                            card_id = match.group(1)
                            item = card_map.get(card_id)
                            if not item:
                                return ""
                            title, original = item
                            seen_image_urls.add(original)
                            title_safe = title.replace("\n", " ").strip() or "image"
                            prefix = ""
                            if match.start() > 0:
                                prev = content[match.start() - 1]
                                if prev not in ("\n", "\r"):
                                    prefix = "\n"
                            return f"{prefix}![{title_safe}]({original})"

                        content = re.sub(
                            r'<grok:render[^>]*card_id="([^"]+)"[^>]*>.*?</grok:render>',
                            _render_card,
                            content,
                            flags=re.DOTALL,
                        )

                    for rendered in await _render_chat_image_refs(
                        self,
                        proc_base._collect_image_references(mr),
                        seen_image_urls,
                        has_streaming_image_event=False,
                    ):
                        if content and not content.endswith("\n"):
                            content += "\n"
                        content += f"{rendered}\n"

                    if card := resp.get("cardAttachment"):
                        streaming_outputs.extend(
                            await _render_chat_image_refs(
                                self,
                                proc_base._collect_image_references(
                                    {"cardAttachment": card}
                                ),
                                seen_image_urls,
                                has_streaming_image_event=False,
                            )
                        )

                    if (
                        (meta := mr.get("metadata", {}))
                        .get("llm_info", {})
                        .get("modelHash")
                    ):
                        fingerprint = meta["llm_info"]["modelHash"]

                if card := resp.get("cardAttachment"):
                    streaming_outputs.extend(
                        await _render_chat_image_refs(
                            self,
                            proc_base._collect_image_references(
                                {"cardAttachment": card}
                            ),
                            seen_image_urls,
                            has_streaming_image_event=False,
                        )
                    )

        except asyncio.CancelledError:
            logger.debug("Collect cancelled by client", extra={"model": self.model})
            raise
        except StreamIdleTimeoutError as e:
            logger.warning(f"Collect idle timeout: {e}", extra={"model": self.model})
            raise UpstreamException(
                message=f"Collect stream idle timeout after {e.idle_seconds}s",
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                    "status": 504,
                },
            )
        except RequestsError as e:
            if proc_base._is_http2_error(e):
                logger.warning(
                    f"HTTP/2 stream error in collect: {e}", extra={"model": self.model}
                )
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    details={"error": str(e), "type": "http2_stream_error", "status": 502},
                )
            logger.error(f"Collect request error: {e}", extra={"model": self.model})
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                details={"error": str(e), "status": 502},
            )
        except Exception as e:
            logger.error(
                f"Collect processing error: {e}",
                extra={"model": self.model, "error_type": type(e).__name__},
            )
            raise
        finally:
            await self.close()

        if streaming_outputs:
            if content and not content.endswith("\n"):
                content += "\n"
            content += "\n".join(streaming_outputs)
            if not content.endswith("\n"):
                content += "\n"

        content = self._filter_content(content)

        # Parse for tool calls if tools were provided
        finish_reason = "stop"
        tool_calls_result = None
        if self.tools and self.tool_choice != "none":
            text_content, tool_calls_list = parse_tool_calls(content, self.tools)
            if tool_calls_list:
                tool_calls_result = tool_calls_list
                content = text_content  # May be None
                finish_reason = "tool_calls"

        message_obj = {
            "role": "assistant",
            "content": content,
            "refusal": None,
            "annotations": [],
        }
        if tool_calls_result:
            message_obj["tool_calls"] = tool_calls_result

        return {
            "id": response_id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": fingerprint,
            "choices": [
                {
                    "index": 0,
                    "message": message_obj,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "prompt_tokens_details": {
                    "cached_tokens": 0,
                    "text_tokens": 0,
                    "audio_tokens": 0,
                    "image_tokens": 0,
                },
                "completion_tokens_details": {
                    "text_tokens": 0,
                    "audio_tokens": 0,
                    "reasoning_tokens": 0,
                },
            },
        }


__all__ = [
    "GrokChatService",
    "MessageExtractor",
    "ChatService",
]
