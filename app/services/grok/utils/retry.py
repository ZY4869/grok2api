"""
Retry helpers for token switching.
"""

from typing import Optional, Set

from app.core.exceptions import UpstreamException
from app.core.logger import logger
from app.services.grok.services.model import ModelService


async def pick_token(
    token_mgr,
    model_id: str,
    tried: Set[str],
    preferred: Optional[str] = None,
    prefer_tags: Optional[Set[str]] = None,
) -> Optional[str]:
    candidate_pools = ModelService.pool_candidates_for_model(model_id)
    primary_pool = candidate_pools[0] if candidate_pools else ""
    dedicated_media = ModelService.is_dedicated_media_model(model_id)

    if preferred and preferred not in tried:
        selectable = True
        preferred_pool = ""
        if hasattr(token_mgr, "get_token_entry") and hasattr(token_mgr, "is_token_selectable"):
            try:
                pool_name, token_info = token_mgr.get_token_entry(preferred)
                preferred_pool = pool_name or ""
                selectable = bool(
                    token_info
                    and pool_name
                    and token_mgr.is_token_selectable(
                        token_info,
                        pool_name,
                        model_id=model_id,
                    )
                )
            except Exception:
                selectable = True
        if selectable:
            if hasattr(token_mgr, "bind_token_context"):
                try:
                    token_mgr.bind_token_context(preferred)
                except Exception:
                    pass
            logger.info(
                "token_pick_selected",
                extra={
                    "model_id": model_id,
                    "candidate_pools": candidate_pools,
                    "selected_pool": preferred_pool,
                    "fallback_from": primary_pool if preferred_pool and preferred_pool != primary_pool else "",
                    "is_dedicated_media_model": dedicated_media,
                    "token": f"{preferred[:10]}...",
                },
            )
            return preferred

    token = None
    for pool_name in candidate_pools:
        token = token_mgr.get_token(
            pool_name,
            exclude=tried,
            prefer_tags=prefer_tags,
            model_id=model_id,
        )
        if token:
            logger.info(
                "token_pick_selected",
                extra={
                    "model_id": model_id,
                    "candidate_pools": candidate_pools,
                    "selected_pool": pool_name,
                    "fallback_from": primary_pool if pool_name != primary_pool else "",
                    "is_dedicated_media_model": dedicated_media,
                    "token": f"{token[:10]}...",
                },
            )
            break

    if not token and not tried:
        result = await token_mgr.refresh_cooling_tokens()
        if result.get("recovered", 0) > 0:
            for pool_name in candidate_pools:
                token = token_mgr.get_token(
                    pool_name,
                    prefer_tags=prefer_tags,
                    model_id=model_id,
                )
                if token:
                    logger.info(
                        "token_pick_selected_after_refresh",
                        extra={
                            "model_id": model_id,
                            "candidate_pools": candidate_pools,
                            "selected_pool": pool_name,
                            "fallback_from": primary_pool if pool_name != primary_pool else "",
                            "is_dedicated_media_model": dedicated_media,
                            "token": f"{token[:10]}...",
                        },
                    )
                    break

    if not token:
        logger.warning(
            "token_pick_unavailable",
            extra={
                "model_id": model_id,
                "candidate_pools": candidate_pools,
                "selected_pool": "",
                "fallback_from": "",
                "is_dedicated_media_model": dedicated_media,
            },
        )

    return token


def rate_limited(error: Exception) -> bool:
    if not isinstance(error, UpstreamException):
        return False
    status = error.details.get("status") if error.details else None
    code = error.details.get("error_code") if error.details else None
    return status == 429 or code == "rate_limit_exceeded"


def bad_request_upstream(error: Exception) -> bool:
    if not isinstance(error, UpstreamException):
        return False
    status = error.details.get("status") if error.details else None
    return status == 400


def summarize_upstream_body(error: Exception, limit: int = 240) -> str:
    if not isinstance(error, UpstreamException):
        return ""
    details = error.details if isinstance(error.details, dict) else {}
    raw = (
        details.get("body_excerpt")
        or details.get("body")
        or details.get("error")
        or ""
    )
    text = str(raw or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def transient_upstream(error: Exception) -> bool:
    """Whether error is likely transient and safe to retry with another token."""
    if not isinstance(error, UpstreamException):
        return False
    details = error.details or {}
    status = details.get("status")
    err = str(details.get("error") or error).lower()
    transient_status = {408, 500, 502, 503, 504}
    if status in transient_status:
        return True
    timeout_markers = (
        "timed out",
        "timeout",
        "connection reset",
        "temporarily unavailable",
        "http2",
    )
    return any(marker in err for marker in timeout_markers)


__all__ = [
    "bad_request_upstream",
    "pick_token",
    "rate_limited",
    "summarize_upstream_body",
    "transient_upstream",
]
