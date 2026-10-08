"""Background schedule for automatic syncs (APScheduler)."""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .sync import NotConfigured, SyncAlreadyRunning, SyncEngine

log = logging.getLogger(__name__)

JOB_ID = "garmin-coros-sync"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Interval options, in minutes.
INTERVAL_CHOICES = [1, 2, 5, 10, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440]

DEFAULT_SCHEDULE: dict[str, Any] = {
    "enabled": False,
    "mode": "interval",  # interval | daily
    "interval_minutes": 360,
    "time": "07:00",
    "days": WEEKDAYS,
}


def validate_schedule(data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data or {})
    # Schedules saved before minute-level intervals stored whole hours.
    legacy_hours = data.pop("interval_hours", None)
    if legacy_hours is not None and "interval_minutes" not in data:
        data["interval_minutes"] = int(legacy_hours) * 60
    sched = {**DEFAULT_SCHEDULE, **data}
    sched["enabled"] = bool(sched["enabled"])
    if sched["mode"] not in {"interval", "daily"}:
        raise ValueError("mode must be 'interval' or 'daily'")
    minutes = int(sched["interval_minutes"])
    if minutes not in INTERVAL_CHOICES:
        raise ValueError(f"interval_minutes must be one of {INTERVAL_CHOICES}")
    sched["interval_minutes"] = minutes
    try:
        hh, mm = (int(x) for x in str(sched["time"]).split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception as exc:  # noqa: BLE001
        raise ValueError("time must be HH:MM") from exc
    sched["time"] = f"{hh:02d}:{mm:02d}"
    days = [d for d in WEEKDAYS if d in set(sched.get("days") or [])]
    if sched["mode"] == "daily" and not days:
        raise ValueError("Pick at least one day")
    sched["days"] = days
    return sched


class SyncScheduler:
    def __init__(self, engine: SyncEngine) -> None:
        self.engine = engine
        self.scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1})

    def start(self) -> None:
        self.scheduler.start()
        self.apply(self.get())

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    @staticmethod
    def get() -> dict[str, Any]:
        return validate_schedule(db.get_setting("schedule", DEFAULT_SCHEDULE))

    def update(self, data: dict[str, Any]) -> dict[str, Any]:
        sched = validate_schedule(data)
        db.set_setting("schedule", sched)
        self.apply(sched)
        return sched

    def apply(self, sched: dict[str, Any]) -> None:
        if self.scheduler.get_job(JOB_ID):
            self.scheduler.remove_job(JOB_ID)
        if not sched["enabled"]:
            return
        if sched["mode"] == "interval":
            trigger = IntervalTrigger(minutes=sched["interval_minutes"])
        else:
            hh, mm = sched["time"].split(":")
            trigger = CronTrigger(hour=int(hh), minute=int(mm), day_of_week=",".join(sched["days"]))
        self.scheduler.add_job(self._tick, trigger, id=JOB_ID, misfire_grace_time=3600)
        log.info("Scheduled sync: %s", sched)

    def next_run(self) -> str | None:
        job = self.scheduler.get_job(JOB_ID)
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
        return None

    def _tick(self) -> None:
        try:
            self.engine.start("scheduled")
        except SyncAlreadyRunning:
            log.info("Scheduled sync skipped: another sync is running")
        except NotConfigured as exc:
            log.warning("Scheduled sync skipped: %s", exc)
