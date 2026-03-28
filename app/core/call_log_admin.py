from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable

from app.core.config import get_config

QUICK_IMAGE_LIMIT_MODELS = {"grok-auto", "grok-3-fast", "grok-4-expert"}
QUICK_IMAGE_LIMIT_EN_MESSAGE = (
    "You've reached your image generation limit. Please try again later."
)


@lru_cache(maxsize=1)
def _image_limit_markers() -> tuple[str, str, str]:
    from app.services.token.quota import (
        IMAGE_LIMIT_ALL_MESSAGE,
        IMAGE_LIMIT_ERROR_CODE,
        IMAGE_LIMIT_SINGLE_MESSAGE,
    )

    return IMAGE_LIMIT_ERROR_CODE, IMAGE_LIMIT_SINGLE_MESSAGE, IMAGE_LIMIT_ALL_MESSAGE


def mask_token(token: str) -> str:
    value = str(token or "").replace("sso=", "").strip()
    if not value:
        return ""
    if len(value) <= 24:
        return value
    return f"{value[:8]}...{value[-16:]}"


def build_token_snapshot(token_mgr: Any) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    consumed_mode = bool(get_config("token.consumed_mode_enabled", False))

    for pool_name, pool in getattr(token_mgr, "pools", {}).items():
        for info in pool.list():
            raw_token = str(getattr(info, "token", "") or "").replace("sso=", "").strip()
            if not raw_token:
                continue
            available = bool(
                getattr(info, "alive", None) is not False and info.is_available(consumed_mode)
            )
            current = snapshot.get(raw_token)
            payload = {
                "token": raw_token,
                "token_masked": mask_token(raw_token),
                "email": str(getattr(info, "email", "") or "").strip(),
                "pool": str(pool_name or "").strip(),
                "status": str(getattr(info, "status", "") or "").strip(),
                "available": available,
            }
            if not current:
                snapshot[raw_token] = payload
                continue
            if not current.get("email") and payload["email"]:
                current["email"] = payload["email"]
            if not current.get("pool") and payload["pool"]:
                current["pool"] = payload["pool"]
            current["available"] = bool(current.get("available")) or available
    return snapshot


def build_account_keyword_tokens(
    keyword: str, token_snapshot: dict[str, dict[str, Any]] | None
) -> list[str]:
    needle = str(keyword or "").strip().lower()
    if not needle or not token_snapshot:
        return []
    matched: list[str] = []
    for token, entry in token_snapshot.items():
        email = str(entry.get("email") or "").lower()
        pool = str(entry.get("pool") or "").lower()
        if needle in email or needle in pool:
            matched.append(token)
    return matched


def enrich_call_log_record(
    record: dict[str, Any], token_snapshot: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    payload = dict(record or {})
    raw_token = str(payload.get("token") or "").replace("sso=", "").strip()
    entry = (token_snapshot or {}).get(raw_token, {})

    email = str(payload.get("email") or "").strip() or str(entry.get("email") or "").strip()
    pool = str(payload.get("pool") or "").strip() or str(entry.get("pool") or "").strip()
    token_masked = str(entry.get("token_masked") or "").strip() or mask_token(raw_token)

    payload["token"] = raw_token
    payload["email"] = email
    payload["pool"] = pool
    payload["token_masked"] = token_masked
    payload["account_display"] = email or token_masked or "未分配账号"
    return payload


def enrich_call_log_records(
    records: Iterable[dict[str, Any]], token_snapshot: dict[str, dict[str, Any]] | None
) -> list[dict[str, Any]]:
    return [enrich_call_log_record(record, token_snapshot) for record in records]


def _record_account_key(record: dict[str, Any]) -> str:
    token = str(record.get("token") or "").strip()
    pool = str(record.get("pool") or "").strip()
    email = str(record.get("email") or "").strip()
    if token:
        return f"token:{token}|pool:{pool}"
    if email:
        return f"email:{email}|pool:{pool}"
    return ""


def is_quick_image_limit_record(record: dict[str, Any]) -> bool:
    if str(record.get("status") or "").strip().lower() != "fail":
        return False
    if str(record.get("model") or "").strip() not in QUICK_IMAGE_LIMIT_MODELS:
        return False

    limit_error_code, single_message, all_message = _image_limit_markers()
    error_code = str(record.get("error_code") or "").strip()
    if error_code == limit_error_code:
        return True

    message = str(record.get("error_message") or "").strip().lower()
    if not message:
        return False

    return any(
        candidate.lower() in message
        for candidate in (
            QUICK_IMAGE_LIMIT_EN_MESSAGE,
            single_message,
            all_message,
        )
    )


def build_quick_image_limit_stats(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items_by_account: dict[str, dict[str, Any]] = {}
    total_hits = 0

    for record in records:
        if not is_quick_image_limit_record(record):
            continue
        total_hits += 1
        account_key = _record_account_key(record) or f"trace:{record.get('trace_id') or total_hits}"
        current = items_by_account.get(account_key)
        if not current:
            current = {
                "email": str(record.get("email") or "").strip(),
                "token": str(record.get("token") or "").strip(),
                "token_masked": str(record.get("token_masked") or "").strip(),
                "pool": str(record.get("pool") or "").strip(),
                "hit_count": 0,
                "last_hit_at": 0,
                "last_error_message": "",
            }
            items_by_account[account_key] = current

        current["hit_count"] += 1
        created_at = int(record.get("created_at") or 0)
        if created_at >= int(current.get("last_hit_at") or 0):
            current["last_hit_at"] = created_at
            current["last_error_message"] = str(record.get("error_message") or "").strip()

    items = sorted(
        items_by_account.values(),
        key=lambda item: (-int(item.get("hit_count") or 0), -int(item.get("last_hit_at") or 0)),
    )
    return {
        "total_hits": total_hits,
        "unique_accounts": len(items),
        "items": items,
    }


def build_account_stats(
    records: Iterable[dict[str, Any]],
    token_snapshot: dict[str, dict[str, Any]] | None,
    quick_limit_stats: dict[str, Any] | None = None,
) -> dict[str, int]:
    called_accounts = {key for key in (_record_account_key(record) for record in records) if key}
    snapshot = token_snapshot or {}
    quick_stats = quick_limit_stats or {"unique_accounts": 0}
    return {
        "total_accounts": len(snapshot),
        "available_accounts": sum(1 for entry in snapshot.values() if entry.get("available")),
        "limit_accounts": int(quick_stats.get("unique_accounts") or 0),
        "called_accounts": len(called_accounts),
    }


__all__ = [
    "QUICK_IMAGE_LIMIT_MODELS",
    "build_account_keyword_tokens",
    "build_account_stats",
    "build_quick_image_limit_stats",
    "build_token_snapshot",
    "enrich_call_log_record",
    "enrich_call_log_records",
    "is_quick_image_limit_record",
    "mask_token",
]
