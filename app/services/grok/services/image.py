"""
Grok image services.
"""

import asyncio
import base64
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterable, Dict, List, Optional, Union

import orjson

from app.core.config import get_config
from app.core.logger import logger
from app.core.storage import DATA_DIR
from app.core.exceptions import AppException, ErrorType, UpstreamException
from app.services.grok.services.chat import GrokChatService
from app.services.grok.services.image_edit import (
    ImageCollectProcessor as AppChatImageCollectProcessor,
    ImageStreamProcessor as AppChatImageStreamProcessor,
)
from app.services.grok.utils.process import BaseProcessor
from app.services.grok.utils.retry import pick_token, rate_limited
from app.services.grok.utils.response import make_response_id, make_chat_chunk, wrap_image_content
from app.services.grok.utils.stream import wrap_stream_with_usage
from app.services.token import EffortType
from app.services.token.quota import (
    RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED,
    RATE_LIMIT_ACTION_RETRY_SAME_TOKEN,
    all_candidate_tokens_exhausted,
    confirm_quota_exhausted,
    image_limit_exception,
    quota_requirement_for_model,
    resolve_rate_limit_hit,
    select_token_for_requirement,
)
from app.services.reverse.app_chat import (
    APP_CHAT_REQUEST_LEGACY_MODEL,
    APP_CHAT_REQUEST_MODEL_ID_AUTO,
)
from app.services.reverse.ws_imagine import ImagineWebSocketReverse


image_service = ImagineWebSocketReverse()
APP_CHAT_IMAGE_REQUEST_STRATEGIES = (
    APP_CHAT_REQUEST_MODEL_ID_AUTO,
    APP_CHAT_REQUEST_LEGACY_MODEL,
)


def _should_probe_image_limit(error: Exception) -> bool:
    if not isinstance(error, UpstreamException):
        return False
    details = error.details if isinstance(error.details, dict) else {}
    code = details.get("error_code")
    marker = details.get("error")
    return rate_limited(error) or code == "blocked_no_final_image" or marker == "empty_result"


@dataclass
class ImageGenerationResult:
    stream: bool
    data: Union[AsyncGenerator[str, None], List[str]]
    usage_override: Optional[dict] = None


class ImageGenerationService:
    """Image generation orchestration service."""

    async def generate(
        self,
        *,
        token_mgr: Any,
        token: str,
        model_info: Any,
        prompt: str,
        n: int,
        response_format: str,
        size: str,
        aspect_ratio: str,
        stream: bool,
        enable_nsfw: Optional[bool] = None,
        chat_format: bool = False,
    ) -> ImageGenerationResult:
        max_token_retries = int(get_config("retry.max_retry") or 3)
        tried_tokens: set[str] = set()
        exhausted_tokens: set[str] = set()
        last_error: Optional[Exception] = None
        quota_requirement = quota_requirement_for_model(model_info.model_id)

        # resolve nsfw once for routing and upstream
        if enable_nsfw is None:
            enable_nsfw = bool(get_config("image.nsfw"))
        prefer_tags = {"nsfw"} if enable_nsfw else None

        if stream:

            async def _stream_retry() -> AsyncGenerator[str, None]:
                nonlocal last_error
                for attempt in range(max_token_retries):
                    preferred = token if (attempt == 0 and not prefer_tags) else None
                    selection = await select_token_for_requirement(
                        token_mgr,
                        model_info.model_id,
                        tried=tried_tokens,
                        requirement=quota_requirement,
                        preferred=preferred,
                        prefer_tags=prefer_tags,
                        exhausted_tokens=exhausted_tokens,
                    )
                    current_token = selection.token
                    if not current_token:
                        if quota_requirement and all_candidate_tokens_exhausted(
                            selection.total_candidates, exhausted_tokens
                        ):
                            raise image_limit_exception(selection.total_candidates)
                        if last_error:
                            raise last_error
                        raise AppException(
                            message="No available tokens. Please try again later.",
                            error_type=ErrorType.RATE_LIMIT.value,
                            code="rate_limit_exceeded",
                            status_code=429,
                        )

                    tried_tokens.add(current_token)
                    yielded = False
                    try:
                        result = await self._stream_with_fallback(
                            token=current_token,
                            model_info=model_info,
                            prompt=prompt,
                            n=n,
                            response_format=response_format,
                            size=size,
                            aspect_ratio=aspect_ratio,
                            enable_nsfw=enable_nsfw,
                            chat_format=chat_format,
                        )
                        wrapped = wrap_stream_with_usage(
                            result.data,
                            token_mgr,
                            current_token,
                            model_info.model_id,
                        )
                        async for chunk in wrapped:
                            yielded = True
                            yield chunk
                        if not yielded:
                            if quota_requirement and await confirm_quota_exhausted(
                                token_mgr,
                                current_token,
                                quota_requirement,
                                exhausted_tokens,
                            ):
                                if all_candidate_tokens_exhausted(
                                    selection.total_candidates, exhausted_tokens
                                ):
                                    raise image_limit_exception(selection.total_candidates)
                                logger.warning(
                                    "Image stream attempt ended without chunks; trying next token",
                                    extra={
                                        "model": model_info.model_id,
                                        "attempt": attempt + 1,
                                        "token": f"{current_token[:10]}...",
                                    },
                                )
                                continue
                            last_error = UpstreamException(
                                "Image generation returned no stream chunks",
                                details={"error": "empty_stream"},
                            )
                            if attempt + 1 < max_token_retries:
                                continue
                            raise last_error
                        return
                    except UpstreamException as e:
                        last_error = e
                        if quota_requirement and _should_probe_image_limit(e):
                            if await confirm_quota_exhausted(
                                token_mgr,
                                current_token,
                                quota_requirement,
                                exhausted_tokens,
                            ):
                                if all_candidate_tokens_exhausted(
                                    selection.total_candidates, exhausted_tokens
                                ):
                                    raise image_limit_exception(selection.total_candidates)
                                logger.warning(
                                    "Image stream token hit quota limit, trying next token",
                                    extra={
                                        "model": model_info.model_id,
                                        "attempt": attempt + 1,
                                        "token": f"{current_token[:10]}...",
                                    },
                                )
                                continue
                        if rate_limited(e):
                            if yielded:
                                raise
                            resolution = await resolve_rate_limit_hit(
                                token_mgr,
                                current_token,
                                model_info.model_id,
                                requirement=quota_requirement,
                                exhausted_tokens=exhausted_tokens,
                            )
                            if (
                                resolution.action == RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED
                                and all_candidate_tokens_exhausted(
                                    selection.total_candidates, exhausted_tokens
                                )
                            ):
                                raise image_limit_exception(selection.total_candidates)
                            if resolution.action == RATE_LIMIT_ACTION_RETRY_SAME_TOKEN:
                                tried_tokens.discard(current_token)
                                if resolution.retry_after_seconds > 0:
                                    await asyncio.sleep(resolution.retry_after_seconds)
                                logger.info(
                                    f"Token {current_token[:10]}... rate limit cleared by probe, "
                                    f"retrying same token (attempt {attempt + 1}/{max_token_retries})"
                                )
                            else:
                                logger.warning(
                                    f"Token {current_token[:10]}... rate limited (429), "
                                    f"trying next token (attempt {attempt + 1}/{max_token_retries})"
                                )
                            continue
                        raise

                if last_error:
                    raise last_error
                raise AppException(
                    message="No available tokens. Please try again later.",
                    error_type=ErrorType.RATE_LIMIT.value,
                    code="rate_limit_exceeded",
                    status_code=429,
                )

            return ImageGenerationResult(stream=True, data=_stream_retry())

        for attempt in range(max_token_retries):
            preferred = token if (attempt == 0 and not prefer_tags) else None
            selection = await select_token_for_requirement(
                token_mgr,
                model_info.model_id,
                tried=tried_tokens,
                requirement=quota_requirement,
                preferred=preferred,
                prefer_tags=prefer_tags,
                exhausted_tokens=exhausted_tokens,
            )
            current_token = selection.token
            if not current_token:
                if quota_requirement and all_candidate_tokens_exhausted(
                    selection.total_candidates, exhausted_tokens
                ):
                    raise image_limit_exception(selection.total_candidates)
                if last_error:
                    raise last_error
                raise AppException(
                    message="No available tokens. Please try again later.",
                    error_type=ErrorType.RATE_LIMIT.value,
                    code="rate_limit_exceeded",
                    status_code=429,
                )

            tried_tokens.add(current_token)
            try:
                return await self._collect_with_fallback(
                    token_mgr=token_mgr,
                    token=current_token,
                    model_info=model_info,
                    tried_tokens=tried_tokens,
                    prompt=prompt,
                    n=n,
                    response_format=response_format,
                    aspect_ratio=aspect_ratio,
                    enable_nsfw=enable_nsfw,
                )
            except UpstreamException as e:
                last_error = e
                if quota_requirement and _should_probe_image_limit(e):
                    if await confirm_quota_exhausted(
                        token_mgr,
                        current_token,
                        quota_requirement,
                        exhausted_tokens,
                    ):
                        if all_candidate_tokens_exhausted(
                            selection.total_candidates, exhausted_tokens
                        ):
                            raise image_limit_exception(selection.total_candidates)
                        logger.warning(
                            "Image collect token hit quota limit, trying next token",
                            extra={
                                "model": model_info.model_id,
                                "attempt": attempt + 1,
                                "token": f"{current_token[:10]}...",
                            },
                        )
                        continue
                if rate_limited(e):
                    resolution = await resolve_rate_limit_hit(
                        token_mgr,
                        current_token,
                        model_info.model_id,
                        requirement=quota_requirement,
                        exhausted_tokens=exhausted_tokens,
                    )
                    if (
                        resolution.action == RATE_LIMIT_ACTION_CONFIRMED_EXHAUSTED
                        and all_candidate_tokens_exhausted(
                            selection.total_candidates, exhausted_tokens
                        )
                    ):
                        raise image_limit_exception(selection.total_candidates)
                    if resolution.action == RATE_LIMIT_ACTION_RETRY_SAME_TOKEN:
                        tried_tokens.discard(current_token)
                        if resolution.retry_after_seconds > 0:
                            await asyncio.sleep(resolution.retry_after_seconds)
                        logger.info(
                            f"Token {current_token[:10]}... rate limit cleared by probe, "
                            f"retrying same token (attempt {attempt + 1}/{max_token_retries})"
                        )
                    else:
                        logger.warning(
                            f"Token {current_token[:10]}... rate limited (429), "
                            f"trying next token (attempt {attempt + 1}/{max_token_retries})"
                        )
                    continue
                raise

        if last_error:
            raise last_error
        raise AppException(
            message="No available tokens. Please try again later.",
            error_type=ErrorType.RATE_LIMIT.value,
            code="rate_limit_exceeded",
            status_code=429,
        )

    @staticmethod
    def _build_request_overrides(n: int, enable_nsfw: bool) -> Dict[str, Any]:
        return {
            "imageGenerationCount": max(1, int(n or 1)),
            "enableNsfw": bool(enable_nsfw),
        }

    @staticmethod
    def _parse_sse_chunk(chunk: str) -> tuple[str, Any]:
        event = ""
        data_lines: List[str] = []
        for line in (chunk or "").splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())

        if not data_lines:
            return event, None

        raw = "\n".join(data_lines).strip()
        if raw == "[DONE]":
            return event, raw

        try:
            return event, orjson.loads(raw)
        except orjson.JSONDecodeError:
            return event, raw

    @classmethod
    def _is_meaningful_stream_chunk(
        cls, chunk: str, response_format: str, chat_format: bool
    ) -> bool:
        event, payload = cls._parse_sse_chunk(chunk)
        if chat_format:
            if event != "chat.completion.chunk" or not isinstance(payload, dict):
                return False
            for choice in payload.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content.strip():
                    return True
            return False

        if event != "image_generation.completed" or not isinstance(payload, dict):
            return False

        field = "url" if response_format == "url" else "b64_json"
        value = payload.get(field)
        return isinstance(value, str) and bool(value.strip())

    async def _stream_with_fallback(
        self,
        *,
        token: str,
        model_info: Any,
        prompt: str,
        n: int,
        response_format: str,
        size: str,
        aspect_ratio: str,
        enable_nsfw: Optional[bool] = None,
        chat_format: bool = False,
    ) -> ImageGenerationResult:
        async def _combined() -> AsyncGenerator[str, None]:
            last_error: Exception | None = None

            for request_strategy in APP_CHAT_IMAGE_REQUEST_STRATEGIES:
                logger.info(
                    "Trying app-chat image stream request strategy",
                    extra={
                        "image_protocol_strategy": request_strategy,
                        "model_id": model_info.model_id,
                    },
                )
                saw_valid_chunk = False
                buffered_chunks: List[str] = []

                try:
                    app_chat_result = await self._stream_app_chat(
                        token=token,
                        model_info=model_info,
                        prompt=prompt,
                        n=n,
                        response_format=response_format,
                        size=size,
                        aspect_ratio=aspect_ratio,
                        enable_nsfw=enable_nsfw,
                        chat_format=chat_format,
                        request_strategy=request_strategy,
                    )
                    async for chunk in app_chat_result.data:
                        is_valid = self._is_meaningful_stream_chunk(
                            chunk, response_format, chat_format
                        )
                        if chat_format and not saw_valid_chunk:
                            if is_valid:
                                saw_valid_chunk = True
                                for pending in buffered_chunks:
                                    yield pending
                                buffered_chunks.clear()
                                yield chunk
                            else:
                                buffered_chunks.append(chunk)
                            continue

                        if is_valid:
                            saw_valid_chunk = True
                        yield chunk

                    if saw_valid_chunk:
                        return

                    logger.warning(
                        "App-chat image stream completed without final image",
                        extra={
                            "image_protocol_strategy": request_strategy,
                            "fallback_stage": "app_chat_retry",
                            "model_id": model_info.model_id,
                        },
                    )
                except UpstreamException as e:
                    last_error = e
                    if saw_valid_chunk or rate_limited(e):
                        raise
                    logger.warning(
                        "App-chat image stream failed before first image chunk",
                        extra={
                            "image_protocol_strategy": request_strategy,
                            "fallback_stage": "app_chat_retry",
                            "model_id": model_info.model_id,
                            "error": str(e),
                        },
                    )
                except Exception as e:
                    last_error = e
                    if saw_valid_chunk:
                        raise
                    logger.warning(
                        "App-chat image stream failed before first image chunk",
                        extra={
                            "image_protocol_strategy": request_strategy,
                            "fallback_stage": "app_chat_retry",
                            "model_id": model_info.model_id,
                            "error": str(e),
                        },
                    )

            logger.warning(
                "App-chat image stream exhausted, falling back to ws",
                extra={
                    "image_protocol_strategy": "ws_fallback",
                    "fallback_stage": "ws_fallback",
                    "model_id": model_info.model_id,
                    "error": str(last_error) if last_error else "",
                },
            )
            ws_result = await self._stream_ws(
                token=token,
                model_info=model_info,
                prompt=prompt,
                n=n,
                response_format=response_format,
                size=size,
                aspect_ratio=aspect_ratio,
                enable_nsfw=enable_nsfw,
                chat_format=chat_format,
            )
            async for chunk in ws_result.data:
                yield chunk

        return ImageGenerationResult(stream=True, data=_combined())

    async def _stream_app_chat(
        self,
        *,
        token: str,
        model_info: Any,
        prompt: str,
        n: int,
        response_format: str,
        size: str,
        aspect_ratio: str,
        enable_nsfw: Optional[bool] = None,
        chat_format: bool = False,
        request_strategy: str = APP_CHAT_REQUEST_MODEL_ID_AUTO,
    ) -> ImageGenerationResult:
        if enable_nsfw is None:
            enable_nsfw = bool(get_config("image.nsfw"))

        response = await GrokChatService().chat(
            token=token,
            message=prompt,
            model=model_info.grok_model,
            mode=model_info.model_mode,
            stream=True,
            request_overrides=self._build_request_overrides(n, enable_nsfw),
            tool_overrides={"imageGen": True},
            use_mode_id=model_info.use_mode_id,
            request_strategy=request_strategy,
        )
        processor = AppChatImageStreamProcessor(
            model_info.model_id,
            token,
            n=n,
            response_format=response_format,
            chat_format=chat_format,
        )
        return ImageGenerationResult(
            stream=True,
            data=self._normalize_app_chat_stream(
                processor.process(response),
                response_format=response_format,
                size=size,
                chat_format=chat_format,
            ),
        )

    async def _stream_ws(
        self,
        *,
        token: str,
        model_info: Any,
        prompt: str,
        n: int,
        response_format: str,
        size: str,
        aspect_ratio: str,
        enable_nsfw: Optional[bool] = None,
        chat_format: bool = False,
    ) -> ImageGenerationResult:
        if enable_nsfw is None:
            enable_nsfw = bool(get_config("image.nsfw"))
        stream_retries = int(get_config("image.blocked_parallel_attempts") or 5) + 1
        stream_retries = max(1, min(stream_retries, 10))
        upstream = image_service.stream(
            token=token,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            n=n,
            enable_nsfw=enable_nsfw,
            max_retries=stream_retries,
        )
        processor = ImageWSStreamProcessor(
            model_info.model_id,
            token,
            n=n,
            response_format=response_format,
            size=size,
            chat_format=chat_format,
        )
        return ImageGenerationResult(stream=True, data=processor.process(upstream))

    async def _collect_with_fallback(
        self,
        *,
        token_mgr: Any,
        token: str,
        model_info: Any,
        tried_tokens: set[str],
        prompt: str,
        n: int,
        response_format: str,
        aspect_ratio: str,
        enable_nsfw: Optional[bool] = None,
    ) -> ImageGenerationResult:
        last_error: Exception | None = None

        for request_strategy in APP_CHAT_IMAGE_REQUEST_STRATEGIES:
            logger.info(
                "Trying app-chat image collect request strategy",
                extra={
                    "image_protocol_strategy": request_strategy,
                    "model_id": model_info.model_id,
                },
            )
            try:
                images = await self._collect_app_chat(
                    token=token,
                    model_info=model_info,
                    prompt=prompt,
                    n=n,
                    response_format=response_format,
                    enable_nsfw=enable_nsfw,
                    request_strategy=request_strategy,
                )
                if len(images) >= n:
                    return await self._finalize_collect_result(
                        token_mgr=token_mgr,
                        token=token,
                        model_info=model_info,
                        images=images,
                        n=n,
                    )

                logger.warning(
                    "App-chat image collect returned fewer images than requested",
                    extra={
                        "image_protocol_strategy": request_strategy,
                        "fallback_stage": "app_chat_retry",
                        "model_id": model_info.model_id,
                        "final_images": len(images),
                        "requested": n,
                    },
                )
            except UpstreamException as e:
                last_error = e
                if rate_limited(e):
                    raise
                logger.warning(
                    "App-chat image collect failed before final image",
                    extra={
                        "image_protocol_strategy": request_strategy,
                        "fallback_stage": "app_chat_retry",
                        "model_id": model_info.model_id,
                        "error": str(e),
                    },
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "App-chat image collect failed before final image",
                    extra={
                        "image_protocol_strategy": request_strategy,
                        "fallback_stage": "app_chat_retry",
                        "model_id": model_info.model_id,
                        "error": str(e),
                    },
                )

        logger.warning(
            "App-chat image collect exhausted, falling back to ws",
            extra={
                "image_protocol_strategy": "ws_fallback",
                "fallback_stage": "ws_fallback",
                "model_id": model_info.model_id,
                "error": str(last_error) if last_error else "",
            },
        )

        return await self._collect_ws(
            token_mgr=token_mgr,
            token=token,
            model_info=model_info,
            tried_tokens=tried_tokens,
            prompt=prompt,
            n=n,
            response_format=response_format,
            aspect_ratio=aspect_ratio,
            enable_nsfw=enable_nsfw,
        )

    async def _collect_app_chat(
        self,
        *,
        token: str,
        model_info: Any,
        prompt: str,
        n: int,
        response_format: str,
        enable_nsfw: Optional[bool] = None,
        request_strategy: str = APP_CHAT_REQUEST_MODEL_ID_AUTO,
    ) -> List[str]:
        if enable_nsfw is None:
            enable_nsfw = bool(get_config("image.nsfw"))

        response = await GrokChatService().chat(
            token=token,
            message=prompt,
            model=model_info.grok_model,
            mode=model_info.model_mode,
            stream=True,
            request_overrides=self._build_request_overrides(n, enable_nsfw),
            tool_overrides={"imageGen": True},
            use_mode_id=model_info.use_mode_id,
            request_strategy=request_strategy,
        )
        processor = AppChatImageCollectProcessor(
            model_info.model_id,
            token,
            response_format=response_format,
        )
        return await processor.process(response)

    async def _normalize_app_chat_stream(
        self,
        stream: AsyncIterable[str],
        *,
        response_format: str,
        size: str,
        chat_format: bool,
    ) -> AsyncGenerator[str, None]:
        if chat_format:
            async for chunk in stream:
                yield chunk
            return

        response_field = "url" if response_format == "url" else "b64_json"
        timestamp = int(time.time())

        async for chunk in stream:
            event, payload = self._parse_sse_chunk(chunk)
            if event not in {
                "image_generation.partial_image",
                "image_generation.completed",
            } or not isinstance(payload, dict):
                yield chunk
                continue

            normalized = dict(payload)
            normalized.setdefault("type", event)
            normalized.setdefault("created_at", timestamp)
            normalized.setdefault("size", size)
            normalized.setdefault(
                "image_id", f"app-chat-{normalized.get('index', 0)}"
            )
            normalized.setdefault(response_field, normalized.get(response_field) or "")
            if event == "image_generation.partial_image":
                normalized.setdefault("partial_image_index", 0)
                normalized.setdefault("stage", "progress")
            else:
                normalized.setdefault("stage", "final")

            yield (
                f"event: {event}\n"
                f"data: {orjson.dumps(normalized).decode()}\n\n"
            )

    async def _collect_ws(
        self,
        *,
        token_mgr: Any,
        token: str,
        model_info: Any,
        tried_tokens: set[str],
        prompt: str,
        n: int,
        response_format: str,
        aspect_ratio: str,
        enable_nsfw: Optional[bool] = None,
    ) -> ImageGenerationResult:
        if enable_nsfw is None:
            enable_nsfw = bool(get_config("image.nsfw"))
        all_images: List[str] = []
        seen = set()
        expected_per_call = 6
        calls_needed = max(1, int(math.ceil(n / expected_per_call)))
        calls_needed = min(calls_needed, n)

        async def _fetch_batch(call_target: int, call_token: str):
            stream_retries = int(get_config("image.blocked_parallel_attempts") or 5) + 1
            stream_retries = max(1, min(stream_retries, 10))
            upstream = image_service.stream(
                token=call_token,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                n=call_target,
                enable_nsfw=enable_nsfw,
                max_retries=stream_retries,
            )
            processor = ImageWSCollectProcessor(
                model_info.model_id,
                token,
                n=call_target,
                response_format=response_format,
            )
            return await processor.process(upstream)

        tasks = []
        for i in range(calls_needed):
            remaining = n - (i * expected_per_call)
            call_target = min(expected_per_call, remaining)
            tasks.append(_fetch_batch(call_target, token))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for batch in results:
            if isinstance(batch, Exception):
                logger.warning(f"WS batch failed: {batch}")
                continue
            for img in batch:
                if img not in seen:
                    seen.add(img)
                    all_images.append(img)
                if len(all_images) >= n:
                    break
            if len(all_images) >= n:
                break

        # If upstream likely blocked/reviewed some images, run extra parallel attempts
        # and only keep valid finals selected by ws_imagine classification.
        if len(all_images) < n:
            remaining = n - len(all_images)
            extra_attempts = int(get_config("image.blocked_parallel_attempts") or 5)
            extra_attempts = max(0, min(extra_attempts, 10))
            parallel_enabled = bool(get_config("image.blocked_parallel_enabled", True))
            if extra_attempts > 0:
                logger.warning(
                    f"Image finals insufficient ({len(all_images)}/{n}), running "
                    f"{extra_attempts} recovery attempts for remaining={remaining}, "
                    f"parallel_enabled={parallel_enabled}"
                )
                extra_tasks = []
                if parallel_enabled:
                    recovery_tried = set(tried_tokens)
                    recovery_tokens: List[str] = []
                    for _ in range(extra_attempts):
                        recovery_token = await pick_token(
                            token_mgr,
                            model_info.model_id,
                            recovery_tried,
                        )
                        if not recovery_token:
                            break
                        recovery_tried.add(recovery_token)
                        recovery_tokens.append(recovery_token)

                    if recovery_tokens:
                        logger.info(
                            f"Recovery using {len(recovery_tokens)} distinct tokens"
                        )
                    for recovery_token in recovery_tokens:
                        extra_tasks.append(
                            _fetch_batch(min(expected_per_call, remaining), recovery_token)
                        )
                else:
                    extra_tasks = [
                        _fetch_batch(min(expected_per_call, remaining), token)
                        for _ in range(extra_attempts)
                    ]

                if not extra_tasks:
                    logger.warning("No tokens available for recovery attempts")
                    extra_results = []
                else:
                    extra_results = await asyncio.gather(*extra_tasks, return_exceptions=True)
                for batch in extra_results:
                    if isinstance(batch, Exception):
                        logger.warning(f"WS recovery batch failed: {batch}")
                        continue
                    for img in batch:
                        if img not in seen:
                            seen.add(img)
                            all_images.append(img)
                        if len(all_images) >= n:
                            break
                    if len(all_images) >= n:
                        break
                logger.info(
                    f"Image recovery attempts completed: finals={len(all_images)}/{n}, "
                    f"attempts={extra_attempts}"
                )

        if len(all_images) < n:
            logger.error(
                f"Image generation failed after recovery attempts: finals={len(all_images)}/{n}, "
                f"blocked_parallel_attempts={int(get_config('image.blocked_parallel_attempts') or 5)}"
            )
            raise UpstreamException(
                "Image generation blocked or no valid final image",
                details={
                    "error_code": "blocked_no_final_image",
                    "final_images": len(all_images),
                    "requested": n,
                },
            )

        return await self._finalize_collect_result(
            token_mgr=token_mgr,
            token=token,
            model_info=model_info,
            images=all_images,
            n=n,
        )

    async def _finalize_collect_result(
        self,
        *,
        token_mgr: Any,
        token: str,
        model_info: Any,
        images: List[str],
        n: int,
    ) -> ImageGenerationResult:
        try:
            await token_mgr.consume(token, self._get_effort(model_info))
        except Exception as e:
            logger.warning(f"Failed to consume token: {e}")

        return ImageGenerationResult(
            stream=False,
            data=self._select_images(images, n),
            usage_override=self._zero_usage(),
        )

    @staticmethod
    def _zero_usage() -> dict:
        return {
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
        }

    @staticmethod
    def _get_effort(model_info: Any) -> EffortType:
        return (
            EffortType.HIGH
            if (model_info and model_info.cost.value == "high")
            else EffortType.LOW
        )

    @staticmethod
    def _select_images(images: List[str], n: int) -> List[str]:
        if len(images) >= n:
            return images[:n]
        selected = images.copy()
        while len(selected) < n:
            selected.append("error")
        return selected


class ImageWSBaseProcessor(BaseProcessor):
    """WebSocket image processor base."""

    def __init__(self, model: str, token: str = "", response_format: str = "b64_json"):
        if response_format == "base64":
            response_format = "b64_json"
        super().__init__(model, token)
        self.response_format = response_format
        if response_format == "url":
            self.response_field = "url"
        elif response_format == "base64":
            self.response_field = "base64"
        else:
            self.response_field = "b64_json"
        self._image_dir: Optional[Path] = None

    def _ensure_image_dir(self) -> Path:
        if self._image_dir is None:
            base_dir = DATA_DIR / "tmp" / "image"
            base_dir.mkdir(parents=True, exist_ok=True)
            self._image_dir = base_dir
        return self._image_dir

    def _strip_base64(self, blob: str) -> str:
        if not blob:
            return ""
        if "," in blob and "base64" in blob.split(",", 1)[0]:
            return blob.split(",", 1)[1]
        return blob

    def _normalize_ext(self, ext: Optional[str]) -> Optional[str]:
        if not ext or not isinstance(ext, str):
            return None
        normalized = ext.strip().lower().lstrip(".")
        if normalized == "jpeg":
            normalized = "jpg"
        if normalized in {"png", "jpg", "webp"}:
            return normalized
        return None

    def _split_data_uri(self, blob: str) -> tuple[str, str]:
        if not blob:
            return "", ""
        header = ""
        data = blob
        if "," in blob and "base64" in blob.split(",", 1)[0]:
            header, data = blob.split(",", 1)
        return header.lower(), data

    def _guess_ext_from_header(self, header: str) -> Optional[str]:
        if "image/png" in header:
            return "png"
        if "image/jpeg" in header or "image/jpg" in header:
            return "jpg"
        if "image/webp" in header:
            return "webp"
        return None

    def _guess_ext_from_bytes(self, raw: bytes) -> Optional[str]:
        if not raw:
            return None
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if raw.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return "webp"
        return None

    def _guess_ext(
        self, blob: str, *, raw: bytes = b"", explicit_ext: Optional[str] = None
    ) -> Optional[str]:
        normalized = self._normalize_ext(explicit_ext)
        if normalized:
            return normalized
        header, _ = self._split_data_uri(blob)
        normalized = self._guess_ext_from_header(header)
        if normalized:
            return normalized
        normalized = self._guess_ext_from_bytes(raw)
        if normalized:
            return normalized
        return None

    def _resolve_file_payload(
        self, blob: str, *, is_final: bool, explicit_ext: Optional[str] = None
    ) -> tuple[bytes, str]:
        _, data = self._split_data_uri(blob)
        raw = base64.b64decode(data)
        ext = self._guess_ext(blob, raw=raw, explicit_ext=explicit_ext)
        if not ext:
            ext = "jpg" if is_final else "png"
        return raw, ext

    def _filename(self, image_id: str, is_final: bool, ext: Optional[str] = None) -> str:
        ext = self._normalize_ext(ext)
        if not ext:
            ext = "jpg" if is_final else "png"
        return f"{image_id}.{ext}"

    def _build_file_url(self, filename: str) -> str:
        app_url = get_config("app.app_url")
        if app_url:
            return f"{app_url.rstrip('/')}/v1/files/image/{filename}"
        return f"/v1/files/image/{filename}"

    async def _save_blob(
        self, image_id: str, blob: str, is_final: bool, ext: Optional[str] = None
    ) -> str:
        if not self._strip_base64(blob):
            return ""
        image_dir = self._ensure_image_dir()
        file_bytes, output_ext = self._resolve_file_payload(
            blob,
            is_final=is_final,
            explicit_ext=ext,
        )
        filename = self._filename(image_id, is_final, ext=output_ext)
        filepath = image_dir / filename

        def _write_file():
            with open(filepath, "wb") as f:
                f.write(file_bytes)

        await asyncio.to_thread(_write_file)
        return self._build_file_url(filename)

    def _pick_best(self, existing: Optional[Dict], incoming: Dict) -> Dict:
        if not existing:
            return incoming
        if incoming.get("is_final") and not existing.get("is_final"):
            return incoming
        if existing.get("is_final") and not incoming.get("is_final"):
            return existing
        if incoming.get("blob_size", 0) > existing.get("blob_size", 0):
            return incoming
        return existing

    async def _to_output(self, image_id: str, item: Dict) -> str:
        try:
            if self.response_format == "url":
                return await self._save_blob(
                    image_id,
                    item.get("blob", ""),
                    item.get("is_final", False),
                    ext=item.get("ext"),
                )
            return self._strip_base64(item.get("blob", ""))
        except Exception as e:
            logger.warning(f"Image output failed: {e}")
            return ""


class ImageWSStreamProcessor(ImageWSBaseProcessor):
    """WebSocket image stream processor."""

    def __init__(
        self,
        model: str,
        token: str = "",
        n: int = 1,
        response_format: str = "b64_json",
        size: str = "1024x1024",
        chat_format: bool = False,
    ):
        super().__init__(model, token, response_format)
        self.n = n
        self.size = size
        self.chat_format = chat_format
        self._target_id: Optional[str] = None
        self._index_map: Dict[str, int] = {}
        self._partial_map: Dict[str, int] = {}
        self._initial_sent: set[str] = set()
        self._id_generated: bool = False
        self._response_id: str = ""

    def _assign_index(self, image_id: str) -> Optional[int]:
        if image_id in self._index_map:
            return self._index_map[image_id]
        if len(self._index_map) >= self.n:
            return None
        self._index_map[image_id] = len(self._index_map)
        return self._index_map[image_id]

    def _sse(self, event: str, data: dict) -> str:
        return f"event: {event}\ndata: {orjson.dumps(data).decode()}\n\n"

    async def process(self, response: AsyncIterable[dict]) -> AsyncGenerator[str, None]:
        images: Dict[str, Dict] = {}
        emitted_chat_chunk = False

        async for item in response:
            if item.get("type") == "error":
                message = item.get("error") or "Upstream error"
                code = item.get("error_code") or "upstream_error"
                status = item.get("status")
                if code == "rate_limit_exceeded" or status == 429:
                    raise UpstreamException(message, details=item)
                yield self._sse(
                    "error",
                    {
                        "error": {
                            "message": message,
                            "type": "server_error",
                            "code": code,
                        }
                    },
                )
                return
            if item.get("type") != "image":
                continue

            image_id = item.get("image_id")
            if not image_id:
                continue

            if self.n == 1:
                if self._target_id is None:
                    self._target_id = image_id
                index = 0 if image_id == self._target_id else None
            else:
                index = self._assign_index(image_id)

            images[image_id] = self._pick_best(images.get(image_id), item)

            if index is None:
                continue

            if item.get("stage") != "final":
                # Chat Completions image stream should only expose final results.
                if self.chat_format:
                    continue
                if image_id not in self._initial_sent:
                    self._initial_sent.add(image_id)
                    stage = item.get("stage") or "preview"
                    if stage == "medium":
                        partial_index = 1
                        self._partial_map[image_id] = 1
                    else:
                        partial_index = 0
                        self._partial_map[image_id] = 0
                else:
                    stage = item.get("stage") or "partial"
                    if stage == "preview":
                        continue
                    partial_index = self._partial_map.get(image_id, 0)
                    if stage == "medium":
                        partial_index = max(partial_index, 1)
                    self._partial_map[image_id] = partial_index

                if self.response_format == "url":
                    partial_id = f"{image_id}-{stage}-{partial_index}"
                    partial_out = await self._save_blob(
                        partial_id,
                        item.get("blob", ""),
                        False,
                        ext=item.get("ext"),
                    )
                else:
                    partial_out = self._strip_base64(item.get("blob", ""))

                if self.chat_format and partial_out:
                    partial_out = wrap_image_content(partial_out, self.response_format)

                if not partial_out:
                    continue

                if self.chat_format:
                    # OpenAI ChatCompletion chunk format for partial
                    if not self._id_generated:
                        self._response_id = make_response_id()
                        self._id_generated = True
                    emitted_chat_chunk = True
                    yield self._sse(
                        "chat.completion.chunk",
                        make_chat_chunk(
                            self._response_id,
                            self.model,
                            partial_out,
                            index=index,
                        ),
                    )
                else:
                    # Original image_generation format
                    yield self._sse(
                        "image_generation.partial_image",
                        {
                            "type": "image_generation.partial_image",
                            self.response_field: partial_out,
                            "created_at": int(time.time()),
                            "size": self.size,
                            "index": index,
                            "partial_image_index": partial_index,
                            "image_id": image_id,
                            "stage": stage,
                        },
                    )

        if self.n == 1:
            target_item = images.get(self._target_id) if self._target_id else None
            if target_item and target_item.get("is_final", False):
                selected = [(self._target_id, target_item)]
            elif images:
                selected = [
                    max(
                        images.items(),
                        key=lambda x: (
                            x[1].get("is_final", False),
                            x[1].get("blob_size", 0),
                        ),
                    )
                ]
            else:
                selected = []
        else:
            selected = [
                (image_id, images[image_id])
                for image_id in self._index_map
                if image_id in images and images[image_id].get("is_final", False)
            ]

        for image_id, item in selected:
            if self.response_format == "url":
                final_image_id = image_id
                # Keep original imagine image name for imagine chat stream output.
                if self.model != "grok-imagine-1.0-fast":
                    final_image_id = f"{image_id}-final"
                output = await self._save_blob(
                    final_image_id,
                    item.get("blob", ""),
                    item.get("is_final", False),
                    ext=item.get("ext"),
                )
                if self.chat_format and output:
                    output = wrap_image_content(output, self.response_format)
            else:
                output = await self._to_output(image_id, item)
                if self.chat_format and output:
                    output = wrap_image_content(output, self.response_format)

            if not output:
                continue

            if self.n == 1:
                index = 0
            else:
                index = self._index_map.get(image_id, 0)

            if not self._id_generated:
                self._response_id = make_response_id()
                self._id_generated = True

            if self.chat_format:
                # OpenAI ChatCompletion chunk format
                emitted_chat_chunk = True
                yield self._sse(
                    "chat.completion.chunk",
                    make_chat_chunk(
                        self._response_id,
                        self.model,
                        output,
                        index=index,
                        is_final=True,
                    ),
                )
            else:
                # Original image_generation format
                yield self._sse(
                    "image_generation.completed",
                    {
                        "type": "image_generation.completed",
                        self.response_field: output,
                        "created_at": int(time.time()),
                        "size": self.size,
                        "index": index,
                        "image_id": image_id,
                        "stage": "final",
                        "usage": {
                            "total_tokens": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
                        },
                    },
                )

        if self.chat_format:
            if not self._id_generated:
                self._response_id = make_response_id()
                self._id_generated = True
            if not emitted_chat_chunk:
                yield self._sse(
                    "chat.completion.chunk",
                    make_chat_chunk(
                        self._response_id,
                        self.model,
                        "",
                        index=0,
                        is_final=True,
                    ),
                )
            yield "data: [DONE]\n\n"


class ImageWSCollectProcessor(ImageWSBaseProcessor):
    """WebSocket image non-stream processor."""

    def __init__(
        self, model: str, token: str = "", n: int = 1, response_format: str = "b64_json"
    ):
        super().__init__(model, token, response_format)
        self.n = n

    async def process(self, response: AsyncIterable[dict]) -> List[str]:
        images: Dict[str, Dict] = {}

        async for item in response:
            if item.get("type") == "error":
                message = item.get("error") or "Upstream error"
                raise UpstreamException(message, details=item)
            if item.get("type") != "image":
                continue
            image_id = item.get("image_id")
            if not image_id:
                continue
            images[image_id] = self._pick_best(images.get(image_id), item)

        selected = sorted(
            [item for item in images.values() if item.get("is_final", False)],
            key=lambda x: x.get("blob_size", 0),
            reverse=True,
        )
        if self.n:
            selected = selected[: self.n]

        results: List[str] = []
        for item in selected:
            output = await self._to_output(item.get("image_id", ""), item)
            if output:
                results.append(output)

        return results


__all__ = ["ImageGenerationService"]
