"""
Shared processors and response helpers.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterable, List, Optional, TypeVar

import orjson

from app.core.config import get_config
from app.core.exceptions import StreamIdleTimeoutError
from app.core.logger import logger
from app.services.grok.utils.download import DownloadService

T = TypeVar("T")

_LEGACY_IMAGE_KEYS = {"generatedImageUrls", "imageUrls", "imageURLs"}
_STREAMING_IMAGE_KEYS = {"streamingImageGenerationResponse"}
IMAGE_STAGE_PREVIEW = "preview"
IMAGE_STAGE_FINAL = "final"
IMAGE_STAGE_UNKNOWN = "unknown"
_IMAGE_CHUNK_URL_KEYS = {
    "assetUrl",
    "asset_url",
    "downloadUrl",
    "download_url",
    "imageUrl",
    "image_url",
    "original",
    "signedUrl",
    "signed_url",
    "src",
    "url",
}
_IMAGE_CARD_TYPES = {"render_generated_image", "render_edited_image"}


@dataclass(frozen=True)
class ImageReference:
    url: str
    source_shape: str
    card_type: str = ""
    stage: str = IMAGE_STAGE_UNKNOWN


def _is_http2_error(e: Exception) -> bool:
    """Detect whether the exception is related to an HTTP/2 stream error."""
    err_str = str(e).lower()
    return "http/2" in err_str or "curl: (92)" in err_str or "stream" in err_str


def _normalize_line(line: Any) -> Optional[str]:
    """Normalize an SSE line and drop empty or done sentinels."""
    if line is None:
        return None
    if isinstance(line, (bytes, bytearray)):
        text = line.decode("utf-8", errors="ignore")
    else:
        text = str(line)
    text = text.strip()
    if not text:
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    if text == "[DONE]":
        return None
    return text


def _maybe_parse_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[:1] not in {"{", "["}:
            return None
        try:
            return orjson.loads(text)
        except orjson.JSONDecodeError:
            return None
    return value


_GROK_ASSETS_BASE = "https://assets.grok.com/"


def _normalize_grok_image_url(url: str) -> str:
    """Normalize relative Grok asset paths to absolute URLs."""
    text = (url or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "//")):
        return text
    # Relative path like "users/.../generated/.../image.jpg"
    if "/" in text and ("generated/" in text or "assets" in text):
        return f"{_GROK_ASSETS_BASE}{text.lstrip('/')}"
    return text


def _looks_like_image_ref(value: str) -> bool:
    text = (value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    if text.startswith(("http://", "https://", "/")):
        return True
    if "assets.grok.com" in text:
        return True
    # Relative Grok asset path: "users/.../generated/.../image.jpg"
    if "generated/" in text and "/" in text:
        return True
    return False


def _collect_image_chunk_urls(
    value: Any,
    *,
    add_ref,
    card_type: str,
    source_shape: str = "card_image_chunk",
    stage: str = IMAGE_STAGE_UNKNOWN,
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _LEGACY_IMAGE_KEYS:
                _collect_image_chunk_urls(
                    item,
                    add_ref=add_ref,
                    card_type=card_type,
                    source_shape=source_shape,
                    stage=stage,
                )
                continue
            if key in _IMAGE_CHUNK_URL_KEYS:
                if isinstance(item, list):
                    for url in item:
                        if isinstance(url, str):
                            add_ref(url, source_shape, card_type, stage=stage)
                elif isinstance(item, str):
                    add_ref(item, source_shape, card_type, stage=stage)
            _collect_image_chunk_urls(
                item,
                add_ref=add_ref,
                card_type=card_type,
                source_shape=source_shape,
                stage=stage,
            )
        return

    if isinstance(value, list):
        for item in value:
            _collect_image_chunk_urls(
                item,
                add_ref=add_ref,
                card_type=card_type,
                source_shape=source_shape,
                stage=stage,
            )
        return

    if isinstance(value, str) and _looks_like_image_ref(value):
        add_ref(value, source_shape, card_type, stage=stage)


def _collect_card_images(card: Any, *, add_ref) -> None:
    payload = _maybe_parse_json(card)
    if not isinstance(payload, dict):
        return

    json_data = _maybe_parse_json(payload.get("jsonData", payload))
    if not isinstance(json_data, dict):
        json_data = payload

    card_type = str(json_data.get("type") or payload.get("type") or "").strip()

    image = json_data.get("image") or payload.get("image") or {}
    if isinstance(image, dict):
        original = image.get("original")
        if isinstance(original, str):
            add_ref(original, "card_image_original", card_type or "legacy")

    if card_type in _IMAGE_CARD_TYPES:
        _collect_image_chunk_urls(
            json_data.get("image_chunk"),
            add_ref=add_ref,
            card_type=card_type,
            source_shape="card_image_chunk",
        )


def _collect_image_references(obj: Any) -> List[ImageReference]:
    """Collect image references from both legacy and new app-chat response shapes."""
    refs: List[ImageReference] = []
    seen_index: dict[str, int] = {}

    def _stage_rank(stage: str) -> int:
        normalized = stage or IMAGE_STAGE_UNKNOWN
        if normalized == IMAGE_STAGE_PREVIEW:
            return 0
        if normalized == IMAGE_STAGE_UNKNOWN:
            return 1
        return 2

    def add_ref(
        url: str,
        source_shape: str,
        card_type: str = "",
        *,
        stage: str = IMAGE_STAGE_UNKNOWN,
    ) -> None:
        text = _normalize_grok_image_url((url or "").strip())
        if not _looks_like_image_ref(text):
            return
        existing_index = seen_index.get(text)
        if existing_index is not None:
            existing = refs[existing_index]
            if _stage_rank(stage) > _stage_rank(existing.stage):
                refs[existing_index] = ImageReference(
                    url=existing.url,
                    source_shape=existing.source_shape,
                    card_type=existing.card_type,
                    stage=stage or IMAGE_STAGE_UNKNOWN,
                )
            return
        seen_index[text] = len(refs)
        refs.append(
            ImageReference(
                url=text,
                source_shape=source_shape,
                card_type=str(card_type or ""),
                stage=stage or IMAGE_STAGE_UNKNOWN,
            )
        )

    def walk_legacy(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _LEGACY_IMAGE_KEYS:
                    if isinstance(item, list):
                        for url in item:
                            if isinstance(url, str):
                                add_ref(url, "legacy_image_urls")
                    elif isinstance(item, str):
                        add_ref(item, "legacy_image_urls")
                    continue
                walk_legacy(item)
            return
        if isinstance(value, list):
            for item in value:
                walk_legacy(item)

    def walk_cards(value: Any) -> None:
        if isinstance(value, dict):
            if any(
                key in value
                for key in ("cardAttachment", "cardAttachmentsJson", "jsonData", "image")
            ):
                _collect_card_images(value, add_ref=add_ref)
            card = value.get("cardAttachment")
            if card is not None:
                _collect_card_images(card, add_ref=add_ref)
            attachments = value.get("cardAttachmentsJson")
            if isinstance(attachments, list):
                for raw in attachments:
                    parsed = _maybe_parse_json(raw)
                    if parsed is not None:
                        _collect_card_images(parsed, add_ref=add_ref)
                        walk_cards(parsed)
            for item in value.values():
                walk_cards(item)
            return
        if isinstance(value, list):
            for item in value:
                walk_cards(item)

    def _collect_streaming_images(value: Any) -> None:
        if not isinstance(value, dict):
            _collect_image_chunk_urls(
                value,
                add_ref=add_ref,
                card_type="streaming_image_generation",
                source_shape="streaming_image_generation",
                stage=IMAGE_STAGE_FINAL,
            )
            return

        if "preview" in value:
            _collect_image_chunk_urls(
                value.get("preview"),
                add_ref=add_ref,
                card_type="streaming_image_generation",
                source_shape="streaming_image_generation",
                stage=IMAGE_STAGE_PREVIEW,
            )
        if "final" in value:
            _collect_image_chunk_urls(
                value.get("final"),
                add_ref=add_ref,
                card_type="streaming_image_generation",
                source_shape="streaming_image_generation",
                stage=IMAGE_STAGE_FINAL,
            )

        residual = {
            key: item
            for key, item in value.items()
            if key not in {"preview", "final"}
        }
        if residual:
            _collect_image_chunk_urls(
                residual,
                add_ref=add_ref,
                card_type="streaming_image_generation",
                source_shape="streaming_image_generation",
                stage=IMAGE_STAGE_FINAL,
            )

    def walk_streaming(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _STREAMING_IMAGE_KEYS:
                    _collect_streaming_images(item)
                    continue
                walk_streaming(item)
            return
        if isinstance(value, list):
            for item in value:
                walk_streaming(item)

    walk_legacy(obj)
    walk_cards(obj)
    walk_streaming(obj)
    return refs


def _filter_image_references(
    refs: List[ImageReference],
    *,
    include_preview: bool = True,
    include_final: bool = True,
    include_unknown: bool = True,
) -> List[ImageReference]:
    filtered: List[ImageReference] = []
    for ref in refs:
        stage = ref.stage or IMAGE_STAGE_UNKNOWN
        if stage == IMAGE_STAGE_PREVIEW and not include_preview:
            continue
        if stage == IMAGE_STAGE_FINAL and not include_final:
            continue
        if stage == IMAGE_STAGE_UNKNOWN and not include_unknown:
            continue
        filtered.append(ref)
    return filtered


def _collect_images(
    obj: Any,
    *,
    include_preview: bool = True,
    include_final: bool = True,
    include_unknown: bool = True,
) -> List[str]:
    """Backward-compatible image URL collector."""
    refs = _collect_image_references(obj)
    refs = _filter_image_references(
        refs,
        include_preview=include_preview,
        include_final=include_final,
        include_unknown=include_unknown,
    )
    return [ref.url for ref in refs]


def _collect_image_shapes(obj: Any) -> List[str]:
    return sorted({ref.source_shape for ref in _collect_image_references(obj)})


async def _with_idle_timeout(
    iterable: AsyncIterable[T], idle_timeout: float, model: str = ""
) -> AsyncGenerator[T, None]:
    """
    Wrap an async iterable and raise when it stays idle for too long.
    """
    if idle_timeout <= 0:
        async for item in iterable:
            yield item
        return

    iterator = iterable.__aiter__()

    async def _maybe_aclose(it):
        aclose = getattr(it, "aclose", None)
        if not aclose:
            return
        try:
            await aclose()
        except Exception:
            pass

    while True:
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            yield item
        except asyncio.TimeoutError:
            logger.warning(
                f"Stream idle timeout after {idle_timeout}s",
                extra={"model": model, "idle_timeout": idle_timeout},
            )
            await _maybe_aclose(iterator)
            raise StreamIdleTimeoutError(idle_timeout)
        except asyncio.CancelledError:
            await _maybe_aclose(iterator)
            raise
        except StopAsyncIteration:
            break


class BaseProcessor:
    """Shared processor base."""

    def __init__(self, model: str, token: str = ""):
        self.model = model
        self.token = token
        self.created = int(time.time())
        self.app_url = get_config("app.app_url")
        self._dl_service: Optional[DownloadService] = None

    def _get_dl(self) -> DownloadService:
        """Reuse a single downloader per processor."""
        if self._dl_service is None:
            self._dl_service = DownloadService()
        return self._dl_service

    async def close(self):
        """Release downloader resources."""
        if self._dl_service:
            await self._dl_service.close()
            self._dl_service = None

    async def process_url(self, path: str, media_type: str = "image") -> str:
        """Resolve an asset path or URL into a public URL."""
        dl_service = self._get_dl()
        return await dl_service.resolve_url(path, self.token, media_type)


__all__ = [
    "BaseProcessor",
    "IMAGE_STAGE_FINAL",
    "IMAGE_STAGE_PREVIEW",
    "IMAGE_STAGE_UNKNOWN",
    "ImageReference",
    "_collect_image_references",
    "_collect_image_shapes",
    "_collect_images",
    "_filter_image_references",
    "_is_http2_error",
    "_normalize_line",
    "_with_idle_timeout",
]
