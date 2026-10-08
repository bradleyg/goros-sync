"""Garmin Connect access via the maintained ``garminconnect`` library.

Handles the interactive (MFA-capable) connect flow from the web UI and the
non-interactive token-based login used by sync runs.
"""

from __future__ import annotations

import io
import logging
import shutil
import threading
import zipfile
from dataclasses import dataclass
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .config import GARMIN_TOKEN_DIR

log = logging.getLogger(__name__)


class GarminError(Exception):
    pass


class GarminAuthError(GarminError):
    pass


@dataclass
class ConnectResult:
    status: str  # "connected" | "needs_mfa"
    display_name: str | None = None


def _friendly(exc: Exception) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return "Garmin is rate-limiting logins. Wait a few minutes and try again."
    if "MFA Required" in msg:
        return "Garmin needs a fresh MFA login. Reconnect Garmin from the Connections panel."
    if isinstance(exc, GarminConnectAuthenticationError):
        first = msg.splitlines()[0]
        return f"Garmin login failed: {first}"
    return msg.splitlines()[0]


class GarminService:
    """Owns the on-disk token store and any half-finished MFA login."""

    def __init__(self) -> None:
        self._pending: tuple[Garmin, Any] | None = None
        self._lock = threading.Lock()

    @property
    def tokenstore(self) -> str:
        return str(GARMIN_TOKEN_DIR)

    def has_tokens(self) -> bool:
        return (GARMIN_TOKEN_DIR / "garmin_tokens.json").exists()

    # ----------------------------------------------------- interactive login
    def start_connect(self, email: str, password: str) -> ConnectResult:
        with self._lock:
            self._pending = None
            garmin = Garmin(email, password, return_on_mfa=True)
            try:
                status, state = garmin.login()
            except Exception as exc:  # noqa: BLE001 - surface any library failure to the UI
                raise GarminAuthError(_friendly(exc)) from exc

            if status == "needs_mfa":
                self._pending = (garmin, state)
                return ConnectResult("needs_mfa")

            return self._finish(garmin)

    def submit_mfa(self, code: str) -> ConnectResult:
        with self._lock:
            if not self._pending:
                raise GarminAuthError("No Garmin login is waiting for a code. Start again.")
            garmin, state = self._pending
            try:
                garmin.resume_login(state, code.strip())
            except Exception as exc:  # noqa: BLE001
                # A wrong code leaves the pending session intact for a retry.
                raise GarminAuthError(_friendly(exc)) from exc
            self._pending = None
            return self._finish(garmin, profile_loaded=True)

    def _finish(self, garmin: Garmin, profile_loaded: bool = False) -> ConnectResult:
        try:
            if not profile_loaded:
                # return_on_mfa mode skips profile loading on the no-MFA path.
                garmin._load_profile_and_settings()  # noqa: SLF001
            GARMIN_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
            garmin.client.dump(self.tokenstore)
        except Exception as exc:  # noqa: BLE001
            raise GarminAuthError(_friendly(exc)) from exc
        return ConnectResult("connected", garmin.display_name or garmin.full_name)

    def cancel_pending(self) -> None:
        with self._lock:
            self._pending = None

    @property
    def mfa_pending(self) -> bool:
        return self._pending is not None

    def disconnect(self) -> None:
        self.cancel_pending()
        shutil.rmtree(GARMIN_TOKEN_DIR, ignore_errors=True)

    # --------------------------------------------------- sync-time access
    def client(self, email: str | None, password: str | None) -> Garmin:
        """Resume from saved tokens; fall back to credentials (non-MFA only)."""
        garmin = Garmin(email or None, password or None)
        try:
            garmin.login(self.tokenstore)
        except Exception as exc:  # noqa: BLE001
            raise GarminAuthError(_friendly(exc)) from exc
        return garmin

    @staticmethod
    def list_activities(garmin: Garmin, start_date: str, end_date: str) -> list[dict]:
        try:
            return garmin.get_activities_by_date(start_date, end_date, sortorder="asc") or []
        except GarminConnectAuthenticationError as exc:
            raise GarminAuthError(_friendly(exc)) from exc
        except (GarminConnectConnectionError, GarminConnectTooManyRequestsError) as exc:
            raise GarminError(_friendly(exc)) from exc

    @staticmethod
    def download_fit(garmin: Garmin, activity_id: str) -> tuple[bytes, str]:
        """Return (fit_bytes, filename) for the activity's original file."""
        raw = garmin.download_activity(activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
        if not raw:
            raise GarminError("Garmin returned an empty file")
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                fits = [n for n in zf.namelist() if n.lower().endswith(".fit")]
                if not fits:
                    raise GarminError("Original file is not a FIT file (likely a GPX/TCX import)")
                name = fits[0]
                return zf.read(name), f"{activity_id}.fit"
        # Some endpoints return the raw FIT directly; FIT headers carry ".FIT" at byte 8.
        if raw[8:12] == b".FIT":
            return raw, f"{activity_id}.fit"
        raise GarminError("Unrecognised file format from Garmin")
