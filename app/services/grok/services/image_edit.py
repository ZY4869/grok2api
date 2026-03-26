"""
Grok image edit service.
"""

import asyncio
import os
import random
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator, AsyncIterable, List, Union, Any

import orjson
from curl_cffi.requests.errors import RequestsError

from app.core.config import get_config
from app.core.exceptions import (
    AppException,
    ErrorType,
    UpstreamException,
    StreamIdleTimeoutError,
)
from app.core.logger import logger
from app.services.grok.utils.process import (
    BaseProcessor,
    _is_http2_error,
    _collect_image_references,
    _normalize_line,
    _with_idle_timeout,
)
from app.services.grok.utils.upload import UploadService
from app.services.grok.utils.retry import rate_limited
from app.services.grok.utils.response import make_response_id, make_chat_chunk, wrap_image_content
from app.services.grok.services.chat import GrokChatService
from app.services.grok.services.video import VideoService
from app.services.grok.utils.stream import wrap_stream_with_usage
from app.services.token import EffortType
from app.services.token.quota import (
    all_candidate_tokens_exhausted,
    confirm_quota_exhausted,
    image_limit_exception,
    quota_requirement_for_model,
    select_token_for_requirement,
)
from app.services.reverse.app_chat import (
    APP_CHAT_REQUEST_LEGACY_MODEL,
    APP_CHAT_REQUEST_MODEL_ID_AUTO,
)


APP_CHAT_IMAGE_REQUEST_STRATEGIES = (
    APP_CHAT_REQUEST_MODEL_ID_AUTO,
    APP_CHAT_REQUEST_LEGACY_MODEL,
)


def _should_probe_image_limit(error: Exception) -> bool:
    if not isinstance(error, UpstreamException):
        return False
    details = error.details if isinstance(error.details, dict) else {}
    marker = details.get("error")
    return rate_limited(error) or marker == "empty_result"


@dataclass
class ImageEditResult:
    stream: bool
    data: Union[AsyncGenerator[str, None], List[str]]


class ImageEditService:
    """Image edit orchestration service."""

    async def edit(
        self,
        *,
        token_mgr: Any,
        token: str,
        model_info: Any,
        prompt: str,
        images: List[str],
        n: int,
        response_format: str,
        stream: bool,
        chat_format: bool = False,
    ) -> ImageEditResult:
        if len(images) > 3:
            logger.info(
                "Image edit received %d references; using the most recent 3",
                len(images),
            )
            images = images[-3:]

        max_token_retries = int(get_config("retry.max_retry") or 3)
        tried_tokens: set[str] = set()
        exhausted_tokens: set[str] = set()
        last_error: Exception | None = None
        quota_requirement = quota_requirement_for_model(model_info.model_id)

        async def _prepare_request(current_token: str) -> tuple[dict, dict]:
            image_urls = await self._upload_images(images, current_token)
            parent_post_id = await self._get_parent_post_id(current_token, image_urls)

            model_config_override = {
                "modelMap": {
                    "imageEditModel": "imagine",
                    "imageEditModelConfig": {
                        "imageReferences": image_urls,
                    },
                }
            }
            if parent_post_id:
                model_config_override["modelMap"]["imageEditModelConfig"][
                    "parentPostId"
                ] = parent_post_id
            return {"imageGen": True}, model_config_override

        def _raise_no_token(selection_total: int) -> None:
            if quota_requirement and all_candidate_tokens_exhausted(
                selection_total, exhausted_tokens
            ):
                raise image_limit_exception(selection_total)
            if last_error:
                raise last_error
            raise AppException(
                message="No available tokens. Please try again later.",
                error_type=ErrorType.RATE_LIMIT.value,
                code="rate_limit_exceeded",
                status_code=429,
            )

        if stream:

            async def _stream_retry():
                nonlocal last_error
                for attempt in range(max_token_retries):
                    preferred = token if attempt == 0 else None
                    selection = await select_token_for_requirement(
                        token_mgr,
                        model_info.model_id,
                        tried=tried_tokens,
                        preferred=preferred,
                        requirement=quota_requirement,
                        exhausted_tokens=exhausted_tokens,
                    )
                    current_token = selection.token
                    if not current_token:
                        _raise_no_token(selection.total_candidates)

                    tried_tokens.add(current_token)
                    try:
                        tool_overrides, model_config_override = await _prepare_request(
                            current_token
                        )
                        wrapped = wrap_stream_with_usage(
                            self._stream_with_fallback(
                                token=current_token,
                                prompt=prompt,
                                model_info=model_info,
                                n=n,
                                response_format=response_format,
                                tool_overrides=tool_overrides,
                                model_config_override=model_config_override,
                                chat_format=chat_format,
                            ),
                            token_mgr,
                            current_token,
                            model_info.model_id,
                        )
                        yielded = False
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
                                    "Image edit stream attempt ended without chunks; trying next token",
                                    extra={
                                        "model": model_info.model_id,
                                        "attempt": attempt + 1,
                                        "token": f"{current_token[:10]}...",
                                    },
                                )
                                continue
                            last_error = UpstreamException(
                                "Image edit returned no stream chunks",
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
                                    "Image edit stream token hit quota limit, trying next token",
                                    extra={
                                        "model": model_info.model_id,
                                        "attempt": attempt + 1,
                                        "token": f"{current_token[:10]}...",
                                    },
                                )
                                continue
                        if rate_limited(e):
                            await token_mgr.mark_rate_limited(current_token)
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

            return ImageEditResult(stream=True, data=_stream_retry())

        for attempt in range(max_token_retries):
            preferred = token if attempt == 0 else None
            selection = await select_token_for_requirement(
                token_mgr,
                model_info.model_id,
                tried=tried_tokens,
                preferred=preferred,
                requirement=quota_requirement,
                exhausted_tokens=exhausted_tokens,
            )
            current_token = selection.token
            if not current_token:
                _raise_no_token(selection.total_candidates)

            tried_tokens.add(current_token)
            try:
                tool_overrides, model_config_override = await _prepare_request(
                    current_token
                )
                images_out = await self._collect_images(
                    token=current_token,
                    prompt=prompt,
                    model_info=model_info,
                    n=n,
                    response_format=response_format,
                    tool_overrides=tool_overrides,
                    model_config_override=model_config_override,
                )
                try:
                    effort = (
                        EffortType.HIGH
                        if (model_info and model_info.cost.value == "high")
                        else EffortType.LOW
                    )
                    await token_mgr.consume(current_token, effort)
                    logger.debug(
                        f"Image edit completed, recorded usage (effort={effort.value})"
                    )
                except Exception as e:
                    logger.warning(f"Failed to record image edit usage: {e}")
                return ImageEditResult(stream=False, data=images_out)
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
                            "Image edit token hit quota limit, trying next token",
                            extra={
                                "model": model_info.model_id,
                                "attempt": attempt + 1,
                                "token": f"{current_token[:10]}...",
                            },
                        )
                        continue
                if rate_limited(e):
                    await token_mgr.mark_rate_limited(current_token)
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

    async def _upload_images(self, images: List[str], token: str) -> List[str]:
        image_urls: List[str] = []
        upload_service = UploadService()
        try:
            for image in images:
                _, file_uri = await upload_service.upload_file(image, token)
                if file_uri:
                    if file_uri.startswith("http"):
                        image_urls.append(file_uri)
                    else:
                        image_urls.append(
                            f"https://assets.grok.com/{file_uri.lstrip('/')}"
                        )
        finally:
            await upload_service.close()

        if not image_urls:
            raise AppException(
                message="Image upload failed",
                error_type=ErrorType.SERVER.value,
                code="upload_failed",
            )

        return image_urls

    async def _get_parent_post_id(self, token: str, image_urls: List[str]) -> str:
        parent_post_id = None
        try:
            media_service = VideoService()
            parent_post_id = await media_service.create_image_post(token, image_urls[0])
            logger.debug(f"Parent post ID: {parent_post_id}")
        except Exception as e:
            logger.warning(f"Create image post failed: {e}")

        if parent_post_id:
            return parent_post_id

        for url in image_urls:
            match = re.search(r"/generated/([a-f0-9-]+)/", url)
            if match:
                parent_post_id = match.group(1)
                logger.debug(f"Parent post ID: {parent_post_id}")
                break
            match = re.search(r"/users/[^/]+/([a-f0-9-]+)/content", url)
            if match:
                parent_post_id = match.group(1)
                logger.debug(f"Parent post ID: {parent_post_id}")
                break

        return parent_post_id or ""

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
        prompt: str,
        model_info: Any,
        n: int,
        response_format: str,
        tool_overrides: dict,
        model_config_override: dict,
        chat_format: bool,
    ) -> AsyncGenerator[str, None]:
        last_error: Exception | None = None

        for request_strategy in APP_CHAT_IMAGE_REQUEST_STRATEGIES:
            logger.info(
                "Trying app-chat image edit request strategy",
                extra={
                    "image_protocol_strategy": request_strategy,
                    "model_id": model_info.model_id,
                },
            )
            saw_valid_chunk = False
            buffered_chunks: List[str] = []

            try:
                response = await GrokChatService().chat(
                    token=token,
                    message=prompt,
                    model=model_info.grok_model,
                    mode=None,
                    stream=True,
                    tool_overrides=tool_overrides,
                    model_config_override=model_config_override,
                    request_strategy=request_strategy,
                )
                processor = ImageStreamProcessor(
                    model_info.model_id,
                    token,
                    n=n,
                    response_format=response_format,
                    chat_format=chat_format,
                )

                async for chunk in processor.process(response):
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
                    "App-chat image edit stream completed without final image",
                    extra={
                        "image_protocol_strategy": request_strategy,
                        "fallback_stage": "app_chat_retry",
                        "model_id": model_info.model_id,
                    },
                )
            except UpstreamException as e:
                last_error = e
                if rate_limited(e):
                    raise
                if saw_valid_chunk:
                    raise
                logger.warning(
                    "App-chat image edit stream failed before first image chunk",
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
                    "App-chat image edit stream failed before first image chunk",
                    extra={
                        "image_protocol_strategy": request_strategy,
                        "fallback_stage": "app_chat_retry",
                        "model_id": model_info.model_id,
                        "error": str(e),
                    },
                )

        if last_error:
            raise last_error
        raise UpstreamException(
            "Image edit returned no results", details={"error": "empty_result"}
        )

    async def _collect_images(
        self,
        *,
        token: str,
        prompt: str,
        model_info: Any,
        n: int,
        response_format: str,
        tool_overrides: dict,
        model_config_override: dict,
    ) -> List[str]:
        calls_needed = (n + 1) // 2

        async def _call_edit():
            last_error: Exception | None = None
            for request_strategy in APP_CHAT_IMAGE_REQUEST_STRATEGIES:
                logger.info(
                    "Trying app-chat image edit collect request strategy",
                    extra={
                        "image_protocol_strategy": request_strategy,
                        "model_id": model_info.model_id,
                    },
                )
                try:
                    response = await GrokChatService().chat(
                        token=token,
                        message=prompt,
                        model=model_info.grok_model,
                        mode=None,
                        stream=True,
                        tool_overrides=tool_overrides,
                        model_config_override=model_config_override,
                        request_strategy=request_strategy,
                    )
                    processor = ImageCollectProcessor(
                        model_info.model_id, token, response_format=response_format
                    )
                    images = await processor.process(response)
                    if images:
                        return images
                    logger.warning(
                        "App-chat image edit collect returned no images",
                        extra={
                            "image_protocol_strategy": request_strategy,
                            "fallback_stage": "app_chat_retry",
                            "model_id": model_info.model_id,
                        },
                    )
                except UpstreamException as e:
                    last_error = e
                    if rate_limited(e):
                        raise
                    logger.warning(
                        "App-chat image edit collect failed before final image",
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
                        "App-chat image edit collect failed before final image",
                        extra={
                            "image_protocol_strategy": request_strategy,
                            "fallback_stage": "app_chat_retry",
                            "model_id": model_info.model_id,
                            "error": str(e),
                        },
                    )

            if last_error:
                raise last_error
            return []

        last_error: Exception | None = None
        rate_limit_error: Exception | None = None

        if calls_needed == 1:
            all_images = await _call_edit()
        else:
            tasks = [_call_edit() for _ in range(calls_needed)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_images: List[str] = []
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Concurrent call failed: {result}")
                    last_error = result
                    if rate_limited(result):
                        rate_limit_error = result
                elif isinstance(result, list):
                    all_images.extend(result)

        if not all_images:
            if rate_limit_error:
                raise rate_limit_error
            if last_error:
                raise last_error
            raise UpstreamException(
                "Image edit returned no results", details={"error": "empty_result"}
            )

        if len(all_images) >= n:
            return all_images[:n]

        selected_images = all_images.copy()
        while len(selected_images) < n:
            selected_images.append("error")
        return selected_images


class ImageStreamProcessor(BaseProcessor):
    """HTTP image stream processor."""

    def __init__(
        self, model: str, token: str = "", n: int = 1, response_format: str = "b64_json", chat_format: bool = False
    ):
        super().__init__(model, token)
        self.partial_index = 0
        self.n = n
        self.target_index = 0 if n == 1 else None
        self.response_format = response_format
        self.chat_format = chat_format
        self._id_generated = False
        self._response_id = ""
        if response_format == "url":
            self.response_field = "url"
        elif response_format == "base64":
            self.response_field = "base64"
        else:
            self.response_field = "b64_json"

    def _sse(self, event: str, data: dict) -> str:
        """Build SSE response."""
        return f"event: {event}\ndata: {orjson.dumps(data).decode()}\n\n"

    async def _resolve_image_output(self, ref_url: str) -> str:
        if self.response_format == "url":
            return await self.process_url(ref_url, "image")
        try:
            dl_service = self._get_dl()
            base64_data = await dl_service.parse_b64(ref_url, self.token, "image")
            if base64_data:
                if "," in base64_data:
                    return base64_data.split(",", 1)[1]
                return base64_data
        except Exception as e:
            logger.warning(
                f"Failed to convert image to base64, falling back to URL: {e}"
            )
        return await self.process_url(ref_url, "image")

    async def process(
        self, response: AsyncIterable[bytes]
    ) -> AsyncGenerator[str, None]:
        """Process stream response."""
        final_images = []
        seen_outputs = set()
        emitted_chat_chunk = False
        idle_timeout = get_config("image.stream_timeout")

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                # Image generation progress
                if img := resp.get("streamingImageGenerationResponse"):
                    image_index = img.get("imageIndex", 0)
                    progress = img.get("progress", 0)

                    if self.n == 1 and image_index != self.target_index:
                        continue

                    out_index = 0 if self.n == 1 else image_index

                    if not self.chat_format:
                        yield self._sse(
                            "image_generation.partial_image",
                            {
                                "type": "image_generation.partial_image",
                                self.response_field: "",
                                "index": out_index,
                                "progress": progress,
                            },
                        )
                    continue

                refs = _collect_image_references(resp)
                if refs:
                    logger.debug(
                        "Image edit stream parsed upstream images",
                        extra={
                            "image_response_shape": ",".join(
                                sorted({ref.source_shape for ref in refs})
                            ),
                            "model_id": self.model,
                        },
                    )
                    for ref in refs:
                        output = await self._resolve_image_output(ref.url)
                        if output and output not in seen_outputs:
                            seen_outputs.add(output)
                            final_images.append(output)
                    continue

            for index, img_data in enumerate(final_images):
                if self.n == 1:
                    if index != self.target_index:
                        continue
                    out_index = 0
                else:
                    out_index = index

                # Wrap in markdown format for chat
                output = img_data
                if self.chat_format and output:
                    output = wrap_image_content(output, self.response_format)

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
                            index=out_index,
                            is_final=True,
                        ),
                    )
                else:
                    # Original image_generation format
                    yield self._sse(
                        "image_generation.completed",
                        {
                            "type": "image_generation.completed",
                            self.response_field: img_data,
                            "index": out_index,
                            "usage": {
                                "total_tokens": 0,
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "input_tokens_details": {
                                    "text_tokens": 0,
                                    "image_tokens": 0,
                                },
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
        except asyncio.CancelledError:
            logger.debug("Image stream cancelled by client")
        except StreamIdleTimeoutError as e:
            raise UpstreamException(
                message=f"Image stream idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                },
            )
        except RequestsError as e:
            if _is_http2_error(e):
                logger.warning(f"HTTP/2 stream error in image: {e}")
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"error": str(e), "type": "http2_stream_error"},
                )
            logger.error(f"Image stream request error: {e}")
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(
                f"Image stream processing error: {e}",
                extra={"error_type": type(e).__name__},
            )
            raise
        finally:
            await self.close()


class ImageCollectProcessor(BaseProcessor):
    """HTTP image non-stream processor."""

    def __init__(self, model: str, token: str = "", response_format: str = "b64_json"):
        if response_format == "base64":
            response_format = "b64_json"
        super().__init__(model, token)
        self.response_format = response_format

    async def _resolve_image_output(self, ref_url: str) -> str:
        if self.response_format == "url":
            return await self.process_url(ref_url, "image")
        try:
            dl_service = self._get_dl()
            base64_data = await dl_service.parse_b64(ref_url, self.token, "image")
            if base64_data:
                if "," in base64_data:
                    return base64_data.split(",", 1)[1]
                return base64_data
        except Exception as e:
            logger.warning(
                f"Failed to convert image to base64, falling back to URL: {e}"
            )
        return await self.process_url(ref_url, "image")

    async def process(self, response: AsyncIterable[bytes]) -> List[str]:
        """Process and collect images."""
        images = []
        seen_outputs = set()
        idle_timeout = get_config("image.stream_timeout")

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                refs = _collect_image_references(resp)
                if refs:
                    logger.debug(
                        "Image edit collect parsed upstream images",
                        extra={
                            "image_response_shape": ",".join(
                                sorted({ref.source_shape for ref in refs})
                            ),
                            "model_id": self.model,
                        },
                    )
                    for ref in refs:
                        output = await self._resolve_image_output(ref.url)
                        if output and output not in seen_outputs:
                            seen_outputs.add(output)
                            images.append(output)

        except asyncio.CancelledError:
            logger.debug("Image collect cancelled by client")
        except StreamIdleTimeoutError as e:
            logger.warning(f"Image collect idle timeout: {e}")
        except RequestsError as e:
            if _is_http2_error(e):
                logger.warning(f"HTTP/2 stream error in image collect: {e}")
            else:
                logger.error(f"Image collect request error: {e}")
        except Exception as e:
            logger.error(
                f"Image collect processing error: {e}",
                extra={"error_type": type(e).__name__},
            )
        finally:
            await self.close()

        return images


__all__ = ["ImageEditService", "ImageEditResult"]
