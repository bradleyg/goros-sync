"""Sync engine: pull recent Garmin activities and push their FIT files to COROS."""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta
from typing import Any

from . import db
from .config import DEFAULT_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS
from .coros_client import CorosAuthError, CorosClient, CorosError, CorosSession
from .garmin_service import GarminAuthError, GarminError, GarminService

log = logging.getLogger(__name__)


class SyncAlreadyRunning(Exception):
    pass


class NotConfigured(Exception):
    pass


def _activity_fields(act: dict) -> dict[str, Any]:
    return {
        "garmin_activity_id": str(act.get("activityId")),
        "name": act.get("activityName") or "Untitled activity",
        "activity_type": (act.get("activityType") or {}).get("typeKey"),
        "start_time": act.get("startTimeLocal"),
        "distance_m": act.get("distance"),
        "duration_s": act.get("duration"),
    }


def get_lookback_days() -> int:
    return int(db.get_setting("lookback_days", DEFAULT_LOOKBACK_DAYS))


def build_coros_client() -> CorosClient:
    creds = db.get_setting("coros_credentials")
    if not creds:
        raise NotConfigured("Connect your COROS account first.")
    session_data = db.get_setting("coros_session")
    session = CorosSession.from_dict(session_data) if session_data else None
    return CorosClient(creds["email"], creds["password"], session=session)


class SyncEngine:
    def __init__(self, garmin: GarminService) -> None:
        self.garmin = garmin
        self._lock = threading.Lock()
        self._progress: dict[str, Any] = {"running": False}

    # ------------------------------------------------------------ public
    @property
    def progress(self) -> dict[str, Any]:
        return dict(self._progress)

    def is_running(self) -> bool:
        return self._lock.locked()

    def start(self, trigger: str, lookback_days: int | None = None) -> int:
        if not self._lock.acquire(blocking=False):
            raise SyncAlreadyRunning("A sync is already in progress.")
        try:
            if not db.get_setting("garmin_credentials") or not self.garmin.has_tokens():
                raise NotConfigured("Connect your Garmin account first.")
            if not db.get_setting("coros_credentials"):
                raise NotConfigured("Connect your COROS account first.")
            days = max(1, min(int(lookback_days or get_lookback_days()), MAX_LOOKBACK_DAYS))
            start_date = (date.today() - timedelta(days=days - 1)).isoformat()
            run_id = db.create_run(trigger, start_date)
        except Exception:
            self._lock.release()
            raise

        self._progress = {
            "running": True,
            "run_id": run_id,
            "trigger": trigger,
            "phase": "Starting",
            "total": 0,
            "done": 0,
            "current": None,
        }
        thread = threading.Thread(
            target=self._run_guarded, args=(run_id, start_date, trigger), name=f"sync-{run_id}", daemon=True
        )
        thread.start()
        return run_id

    # ----------------------------------------------------------- worker
    def _run_guarded(self, run_id: int, start_date: str, trigger: str) -> None:
        try:
            self._run(run_id, start_date, trigger)
        except Exception as exc:  # noqa: BLE001 - always close out the run
            log.exception("Sync %s crashed", run_id)
            db.update_run(run_id, status="failed", finished_at=db.now_iso(), message=str(exc))
        finally:
            self._progress = {"running": False, "last_run_id": run_id}
            self._lock.release()

    def _phase(self, phase: str, **extra: Any) -> None:
        self._progress.update(phase=phase, **extra)

    def _run(self, run_id: int, start_date: str, trigger: str) -> None:
        counts = {"found": 0, "uploaded": 0, "already": 0, "skipped": 0, "failed": 0}

        def finish(status: str, message: str | None = None) -> None:
            db.update_run(run_id, status=status, finished_at=db.now_iso(), message=message, **counts)

        # 1. Garmin
        self._phase("Signing in to Garmin")
        creds = db.get_setting("garmin_credentials") or {}
        try:
            garmin = self.garmin.client(creds.get("email"), creds.get("password"))
            self._phase("Fetching Garmin activities")
            activities = self.garmin.list_activities(garmin, start_date, date.today().isoformat())
        except GarminAuthError as exc:
            db.set_setting("garmin_status", {"ok": False, "error": str(exc), "at": db.now_iso()})
            return finish("failed", str(exc))
        except GarminError as exc:
            return finish("failed", str(exc))
        db.set_setting("garmin_status", {"ok": True, "error": None, "at": db.now_iso()})

        counts["found"] = len(activities)
        ids = [str(a.get("activityId")) for a in activities]
        done_ids = db.synced_ids(ids)
        todo = [a for a in activities if str(a.get("activityId")) not in done_ids]
        counts["already"] = len(activities) - len(todo)
        db.update_run(run_id, **counts)

        if not todo:
            db.set_setting("last_check", {"at": db.now_iso(), "trigger": trigger, "found": counts["found"]})
            if trigger == "scheduled":
                # Nothing new: keep history readable by not logging no-op scheduled runs.
                db.delete_run(run_id)
                return None
            return finish("success", "Everything is already up to date.")

        # 2. COROS
        self._phase("Signing in to COROS", total=len(todo), done=0)
        try:
            coros = build_coros_client()
            session = coros.ensure_session()
            db.set_setting("coros_session", session.to_dict())
        except (CorosAuthError, CorosError, NotConfigured) as exc:
            db.set_setting("coros_status", {"ok": False, "error": str(exc), "at": db.now_iso()})
            return finish("failed", str(exc))
        db.set_setting("coros_status", {"ok": True, "error": None, "at": db.now_iso()})

        # 3. Transfer each activity
        aborted: str | None = None
        try:
            for index, act in enumerate(todo):
                fields = _activity_fields(act)
                aid = fields["garmin_activity_id"]
                self._phase("Syncing", done=index, current=fields["name"])

                if act.get("manualActivity"):
                    counts["skipped"] += 1
                    db.add_item(run_id, **fields, status="skipped", detail="Manual entry: no recorded file")
                    db.mark_synced(aid, "skipped", run_id, None)
                    continue

                try:
                    fit_bytes, filename = self.garmin.download_fit(garmin, aid)
                except GarminError as exc:
                    counts["skipped"] += 1
                    db.add_item(run_id, **fields, status="skipped", detail=str(exc))
                    db.mark_synced(aid, "skipped", run_id, None)
                    continue
                except Exception as exc:  # noqa: BLE001 - network etc; retry next run
                    counts["failed"] += 1
                    db.add_item(run_id, **fields, status="failed", detail=f"Download failed: {exc}")
                    continue

                try:
                    result = coros.upload_fit(fit_bytes, filename)
                except CorosAuthError as exc:
                    counts["failed"] += 1
                    db.add_item(run_id, **fields, status="failed", detail=str(exc))
                    aborted = str(exc)
                    db.set_setting("coros_status", {"ok": False, "error": str(exc), "at": db.now_iso()})
                    break
                except CorosError as exc:
                    counts["failed"] += 1
                    db.add_item(run_id, **fields, status="failed", detail=str(exc))
                    continue

                if result.success or result.pending:
                    status = "uploaded" if result.success else "pending"
                    counts["uploaded"] += 1
                    db.add_item(
                        run_id, **fields, status=status, detail=result.message, coros_import_id=result.import_id
                    )
                    db.mark_synced(aid, status, run_id, result.import_id)
                else:
                    counts["failed"] += 1
                    db.add_item(
                        run_id, **fields, status="failed", detail=result.message, coros_import_id=result.import_id
                    )
                db.update_run(run_id, **counts)
                time.sleep(1)  # be gentle with both services
        finally:
            coros.close()
            # Persist any refreshed tokens.
            if coros.session:
                db.set_setting("coros_session", coros.session.to_dict())

        self._phase("Finishing", done=len(todo), current=None)
        if aborted:
            return finish("failed", aborted)
        if counts["failed"] and counts["uploaded"]:
            return finish("partial", f"{counts['failed']} activit{'y' if counts['failed'] == 1 else 'ies'} failed.")
        if counts["failed"]:
            return finish("failed", "No activities could be uploaded.")
        return finish("success", None)
