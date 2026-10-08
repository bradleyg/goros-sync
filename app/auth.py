"""Single-user username/password login with signed session cookies.

Enabled when APP_PASSWORD is set. Sessions are stateless HMAC-signed tokens;
the signing key mixes in the configured username and password, so changing
either one signs everybody out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque

from .config import APP_PASSWORD, APP_USERNAME, DATA_DIR, SESSION_DAYS, ensure_data_dir

COOKIE_NAME = "gcs_session"
SESSION_TTL = SESSION_DAYS * 86400

# Brute-force throttle: this many failures per client within the window locks it out for the window.
MAX_FAILURES = 5
MAX_GLOBAL_FAILURES = 30
FAILURE_WINDOW = 15 * 60


def enabled() -> bool:
    return bool(APP_PASSWORD)


def _secret() -> bytes:
    env = os.getenv("SESSION_SECRET")
    if env:
        return env.encode()
    ensure_data_dir()
    # Generated once and kept in the data volume so sessions survive restarts.
    path = DATA_DIR / "session_secret"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key


_key_cache: bytes | None = None


def _key() -> bytes:
    global _key_cache
    if _key_cache is None:
        _key_cache = hmac.new(_secret(), f"{APP_USERNAME}\0{APP_PASSWORD}".encode(), hashlib.sha256).digest()
    return _key_cache


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_credentials(username: str, password: str) -> bool:
    # Compare both fields every time so timing doesn't reveal which one was wrong.
    user_ok = hmac.compare_digest(username.strip().encode(), APP_USERNAME.encode())
    pass_ok = hmac.compare_digest(password.encode(), (APP_PASSWORD or "").encode())
    return enabled() and user_ok and pass_ok


def issue_token() -> str:
    payload = _b64(json.dumps({"u": APP_USERNAME, "exp": int(time.time()) + SESSION_TTL}).encode())
    sig = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.partition(".")
    expected = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(_unb64(payload))
    except Exception:  # noqa: BLE001
        return False
    return data.get("u") == APP_USERNAME and int(data.get("exp", 0)) > time.time()


class LoginThrottle:
    """In-memory failure counters (good enough for a single-process app).

    Counted per client and globally: the client address comes from
    X-Forwarded-For when behind a proxy, which an attacker could rotate.
    """

    GLOBAL = "*"

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _wait(self, key: str, limit: int, now: float) -> int:
        q = self._failures[key]
        while q and now - q[0] > FAILURE_WINDOW:
            q.popleft()
        if len(q) < limit:
            return 0
        return max(1, int(FAILURE_WINDOW - (now - q[0])))

    def retry_after(self, client: str) -> int:
        """Seconds until this client may try again (0 = allowed now)."""
        now = time.time()
        with self._lock:
            return max(self._wait(client, MAX_FAILURES, now), self._wait(self.GLOBAL, MAX_GLOBAL_FAILURES, now))

    def failed(self, client: str) -> None:
        now = time.time()
        with self._lock:
            self._failures[client].append(now)
            self._failures[self.GLOBAL].append(now)

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


throttle = LoginThrottle()
