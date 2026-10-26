"""Signed login tokens (stdlib only) and a simple login lock-out."""
import base64
import hashlib
import hmac
import time

import db

TOKEN_HOURS = 12
_failures = {}          # username -> (count, first_failure_time)
MAX_FAILS, LOCK_SECONDS = 5, 300


def _secret() -> bytes:
    return db.get_setting("secret_key").encode()


def make_token(user_id: int) -> str:
    payload = f"{user_id}:{int(time.time()) + TOKEN_HOURS * 3600}".encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return f"{b64(payload)}.{b64(sig)}"


def read_token(token: str):
    """Return the user row for a valid token, else None."""
    try:
        p64, s64 = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        payload = base64.urlsafe_b64decode(pad(p64))
        sig = base64.urlsafe_b64decode(pad(s64))
        if not hmac.compare_digest(sig, hmac.new(_secret(), payload, hashlib.sha256).digest()):
            return None
        uid, exp = payload.decode().split(":")
        if int(exp) < time.time():
            return None
        return db.q1(
            "SELECT id, username, full_name, role FROM users WHERE id=? AND active=1", (int(uid),)
        )
    except Exception:
        return None


def locked(username: str) -> bool:
    count, first = _failures.get(username, (0, 0))
    if count >= MAX_FAILS and time.time() - first < LOCK_SECONDS:
        return True
    if time.time() - first >= LOCK_SECONDS:
        _failures.pop(username, None)
    return False


def record_failure(username: str):
    count, first = _failures.get(username, (0, time.time()))
    _failures[username] = (count + 1, first)


def clear_failures(username: str):
    _failures.pop(username, None)
