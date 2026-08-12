"""Sessions, password hashing, CSRF and login rate limiting.

Sessions are stateless signed cookies (itsdangerous). The CSRF token is a
per-session random value embedded in the cookie payload and echoed back in a
hidden form field on every POST.
"""
import secrets
import time

import bcrypt
from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config

_serializer: URLSafeTimedSerializer | None = None


def serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(config.get_secret_key(), salt="hb-session")
    return _serializer


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def make_session_token(user_id: int) -> str:
    return serializer().dumps({"uid": user_id, "csrf": secrets.token_hex(16)})


def read_session(request: Request) -> dict | None:
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return None
    try:
        data = serializer().loads(token, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        return None
    if not isinstance(data, dict) or "uid" not in data:
        return None
    return data


def is_secure_request(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


def set_session_cookie(response, request: Request, user_id: int) -> None:
    response.set_cookie(
        config.SESSION_COOKIE,
        make_session_token(user_id),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=is_secure_request(request),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(config.SESSION_COOKIE, path="/")


def check_csrf(request: Request, form_token: str) -> bool:
    session = read_session(request)
    if session is None:
        return False
    expected = session.get("csrf", "")
    return bool(expected) and secrets.compare_digest(expected, form_token or "")


# --- Login rate limiting (in-memory, per client IP) ---------------------------

_failures: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_locked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < config.LOGIN_LOCKOUT_SECONDS]
    _failures[ip] = recent
    return len(recent) >= config.LOGIN_MAX_FAILURES


def record_login_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.time())


def clear_login_failures(ip: str) -> None:
    _failures.pop(ip, None)
