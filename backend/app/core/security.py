from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from ..config import settings

COOKIE_NAME = "echoic_token"
ALGORITHM = "HS256"

# naive in-memory login throttle: ip -> [unix timestamps]
_login_attempts: dict[str, list[float]] = {}
_login_lock = threading.Lock()
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_ATTEMPTS = 5


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int, days: int | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=days or settings.session_days)
    payload = {"sub": str(user_id), "iat": datetime.now(timezone.utc), "exp": expire}
    return jwt.encode(payload, settings.get_secret_key(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> int | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.get_secret_key(), algorithms=[ALGORITHM])
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return None


def check_login_rate(ip: str) -> bool:
    """Return True when the caller is allowed to try logging in."""
    now = time.time()
    with _login_lock:
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        _login_attempts[ip] = attempts
        return len(attempts) < LOGIN_MAX_ATTEMPTS


def register_login_attempt(ip: str) -> None:
    with _login_lock:
        _login_attempts.setdefault(ip, []).append(time.time())


def reset_login_attempts(ip: str) -> None:
    with _login_lock:
        _login_attempts.pop(ip, None)
