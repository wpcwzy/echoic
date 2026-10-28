"""Audit log service (v1.0).

Best-effort, append-only audit trail for administrative and security relevant
mutations. Public API:

    record_audit(db, user, action, *, target_type="", target_id=None,
                 detail=None, ip="", commit=True) -> AuditLog | None
    audit_request(db, request, user, action, *, target_type="", target_id=None,
                  detail=None) -> AuditLog | None

Guarantees & conventions (deliberately chosen so hooks can be sprinkled into
endpoints without touching transactions):

* **Never raises.** Any failure (bad detail, DB error, ...) is logged at warning
  level and swallowed — the business operation must always succeed.
* **Call hooks after the business commit.** ``commit=True`` (default) writes the
  audit row in its own transaction, so it never prolongs or rolls back the
  business mutation. Pass ``commit=False`` only when the caller explicitly wants
  the row to share its transaction (then the caller commits).
* ``detail`` is recursively sanitized: keys that look like secrets
  (password/token/api_key/authorization/cookie/...) become ``"[redacted]"``,
  long strings are truncated and the serialized JSON is size-capped.
* ``user`` may be ``None`` (e.g. failed login attempts); ``username`` is stored
  as a snapshot so the trail survives user deletion.

Action naming follows ``<domain>.<verb>`` (see the hooks in ``app/api/*``), e.g.
``auth.login``, ``recording.upload``, ``recording.trash``, ``user.update``,
``share.create``, ``settings.update``, ``tag.create``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog, User

logger = logging.getLogger(__name__)

#: key fragments that mark a detail value as secret → never persisted
SENSITIVE_KEY_HINTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "jwt",
    "session",
)

MAX_STR_LEN = 300
MAX_LIST_ITEMS = 50
MAX_DEPTH = 4
MAX_DETAIL_CHARS = 4000

_REDACTED = "[redacted]"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _is_sensitive_key(key: Any) -> bool:
    lowered = str(key).strip().lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def _clean_text(value: str) -> str:
    text = _CONTROL_CHARS.sub("", value)
    if len(text) > MAX_STR_LEN:
        return text[: MAX_STR_LEN - 1] + "…"
    return text


def sanitize_detail(value: Any, *, depth: int = 0) -> Any:
    """Recursively strip secrets, bound sizes and keep the result JSON-safe."""
    if depth > MAX_DEPTH:
        return "…"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_LIST_ITEMS:
                result["…"] = f"({len(value) - MAX_LIST_ITEMS} more keys)"
                break
            name = str(key)[:100]
            # Boolean flags such as ``has_password`` / ``has_api_key`` describe
            # the configuration, they never contain the secret itself.
            if _is_sensitive_key(name) and not isinstance(item, bool):
                result[name] = _REDACTED
            else:
                result[name] = sanitize_detail(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        cleaned = [sanitize_detail(item, depth=depth + 1) for item in items[:MAX_LIST_ITEMS]]
        if len(items) > MAX_LIST_ITEMS:
            cleaned.append(f"…({len(items) - MAX_LIST_ITEMS} more)")
        return cleaned
    return _clean_text(str(value))


def _detail_json(detail: Any) -> str:
    if detail is None:
        return "{}"
    if not isinstance(detail, dict):
        detail = {"value": detail}
    sanitized = sanitize_detail(detail)
    try:
        text = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        text = json.dumps({"error": "unserializable detail"}, ensure_ascii=False)
    if len(text) > MAX_DETAIL_CHARS:
        keys = list(sanitized.keys())[:20] if isinstance(sanitized, dict) else []
        text = json.dumps(
            {"truncated": True, "size": len(text), "keys": keys},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return text


def client_ip(request: Any) -> str:
    """Best-effort client IP (X-Forwarded-For first, then X-Real-IP, then socket)."""
    if request is None:
        return ""
    try:
        headers = getattr(request, "headers", None)
        if headers is not None:
            forwarded = headers.get("x-forwarded-for") or ""
            if forwarded:
                return forwarded.split(",")[0].strip()[:64]
            real_ip = headers.get("x-real-ip") or ""
            if real_ip:
                return real_ip.strip()[:64]
        client = getattr(request, "client", None)
        host = getattr(client, "host", None) if client is not None else None
        return str(host or "")[:64]
    except Exception:  # pragma: no cover - never fail on IP extraction
        return ""


def _coerce_target_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_audit(
    db: Session,
    user: User | None,
    action: str,
    *,
    target_type: str = "",
    target_id: Any = None,
    detail: dict[str, Any] | None = None,
    ip: str = "",
    commit: bool = True,
) -> AuditLog | None:
    """Append one audit entry. Never raises; returns the row (or ``None``)."""
    if not action:
        return None
    entry: AuditLog | None = None
    try:
        entry = AuditLog(
            user_id=getattr(user, "id", None),
            username=str(getattr(user, "username", "") or "")[:64],
            action=str(action)[:64],
            target_type=str(target_type or "")[:32],
            target_id=_coerce_target_id(target_id),
            detail_json=_detail_json(detail),
            ip=str(ip or "")[:64],
        )
        db.add(entry)
        if commit:
            db.commit()
            db.refresh(entry)
        else:
            db.flush()
        return entry
    except Exception:  # noqa: BLE001 - auditing must never break the operation
        logger.warning("audit write failed (action=%s, target=%s)", action, target_type, exc_info=True)
        if commit:
            try:
                db.rollback()
            except Exception:  # pragma: no cover
                pass
        return None


def audit_request(
    db: Session,
    request: Any,
    user: User | None,
    action: str,
    *,
    target_type: str = "",
    target_id: Any = None,
    detail: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditLog | None:
    """``record_audit`` + client IP extraction; also never raises."""
    try:
        return record_audit(
            db,
            user,
            action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            ip=client_ip(request),
            commit=commit,
        )
    except Exception:  # pragma: no cover - record_audit already swallows
        logger.warning("audit_request failed (action=%s)", action, exc_info=True)
        return None


__all__ = [
    "SENSITIVE_KEY_HINTS",
    "audit_request",
    "client_ip",
    "record_audit",
    "sanitize_detail",
]
