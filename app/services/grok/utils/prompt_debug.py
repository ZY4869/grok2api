"""
Prompt diagnostics helpers for quick-mode image requests.
"""

import hashlib
import re
from typing import Any, Dict, Iterable, List


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PRIMARY_IMAGE_INTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "english_generate_image": re.compile(
        r"\b(generate|create|make|draw|paint|illustrate|render|design)\b.{0,24}\b(image|picture|photo|art|illustration|poster|avatar|wallpaper|logo)\b",
        re.IGNORECASE,
    ),
    "english_image_subject": re.compile(
        r"\b(image|picture|photo|art|illustration|poster|avatar|wallpaper|logo)\b.{0,24}\b(of|for|showing|with)\b",
        re.IGNORECASE,
    ),
    "english_visual_verb_short": re.compile(
        r"^\s*(generate|create|draw|paint|illustrate|render|design)\b\s+(?!.*\b(code|report|list|file|text|summary|docs?|plan|test|function|class|table|query|email)\b).{2,60}$",
        re.IGNORECASE,
    ),
    "cn_generate_visual": re.compile(
        r"(?:\u751f\u6210|\u5236\u4f5c|\u753b|\u7ed8|\u51fa)(?:.{0,10})(?:\u56fe\u7247|\u56fe\u50cf|\u63d2\u753b|\u7acb\u7ed8|\u5934\u50cf|\u6d77\u62a5|\u58c1\u7eb8)",
        re.IGNORECASE,
    ),
    "cn_image_command": re.compile(
        r"(?:\u56fe\u7247|\u56fe\u50cf)\u751f\u6210|(?:\u753b\u56fe|\u7ed8\u56fe|\u51fa\u56fe)|(?:\u753b\u4e00\u5f20|\u751f\u6210\u4e00\u5f20)(?:.{0,10})(?:\u56fe|\u56fe\u7247|\u63d2\u753b|\u7acb\u7ed8|\u5934\u50cf|\u6d77\u62a5|\u58c1\u7eb8)",
        re.IGNORECASE,
    ),
    "cn_visual_verb_object": re.compile(
        r"(生成|画|绘制|帮我画|帮我生成|请生成|请画)\s*(一个|一张|一幅|个|张)\S",
        re.IGNORECASE,
    ),
}
_SECONDARY_IMAGE_HINT_PATTERNS: dict[str, re.Pattern[str]] = {
    "cn_visual_style": re.compile(
        r"(?:Q\u7248|\u4e8c\u6b21\u5143|\u63d2\u753b|\u7acb\u7ed8|\u5934\u50cf|\u6d77\u62a5|\u58c1\u7eb8)",
        re.IGNORECASE,
    ),
    "aspect_ratio": re.compile(r"\b(?:1:1|16:9|9:16|3:2|2:3)\b", re.IGNORECASE),
}


def _coerce_mapping_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _iter_text_fragments(content: Any) -> Iterable[str]:
    if content is None:
        return

    if isinstance(content, str):
        text = content.strip()
        if text:
            yield text
        return

    if isinstance(content, dict):
        content = [content]

    if not isinstance(content, list):
        text = str(content).strip()
        if text:
            yield text
        return

    for item in content:
        if not isinstance(item, dict):
            text = str(item).strip()
            if text:
                yield text
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type in {"", "text", "input_text", "output_text"}:
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                yield text
            continue
        if item_type == "message":
            yield from _iter_text_fragments(item.get("content"))
            continue
        if "content" in item:
            yield from _iter_text_fragments(item.get("content"))


def extract_last_user_text(messages: List[Any]) -> str:
    for message in reversed(messages or []):
        role = str(_coerce_mapping_value(message, "role", "user") or "user")
        if role != "user":
            continue
        parts = list(_iter_text_fragments(_coerce_mapping_value(message, "content")))
        combined = "\n".join(parts).strip()
        if combined:
            return combined
    return ""


def summarize_prompt_text(text: Any) -> Dict[str, Any]:
    value = "" if text is None else str(text)
    categories = detect_image_keyword_categories(value)
    return {
        "message_hash": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
        "message_len": len(value),
        "non_ascii_count": sum(1 for char in value if ord(char) > 127),
        "has_cjk": bool(_CJK_RE.search(value)),
        "has_image_keywords": bool(categories),
        "image_keyword_categories": categories,
    }


def summarize_chat_messages(messages: List[Any]) -> Dict[str, Any]:
    return summarize_prompt_text(extract_last_user_text(messages))


def detect_image_keyword_categories(text: Any) -> List[str]:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return []

    categories: List[str] = []
    for name, pattern in _PRIMARY_IMAGE_INTENT_PATTERNS.items():
        if pattern.search(compact):
            categories.append(name)
    for name, pattern in _SECONDARY_IMAGE_HINT_PATTERNS.items():
        if pattern.search(compact):
            categories.append(name)
    return categories


def looks_like_image_prompt(text: Any) -> bool:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return False
    return any(pattern.search(compact) for pattern in _PRIMARY_IMAGE_INTENT_PATTERNS.values())


__all__ = [
    "detect_image_keyword_categories",
    "extract_last_user_text",
    "looks_like_image_prompt",
    "summarize_chat_messages",
    "summarize_prompt_text",
]
