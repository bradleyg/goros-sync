"""FastAPI app: JSON API + static single-page UI."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import db
from .config import APP_PASSWORD, MAX_LOOKBACK_DAYS, STATIC_DIR
from .coros_client import CorosClient, CorosError
from .garmin_service import GarminAuthError, GarminService
from .scheduler import INTERVAL_CHOICES, SyncScheduler
from .sync import NotConfigured, SyncAlreadyRunning, SyncEngine, get_lookback_days

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

garmin_service = GarminService()
engine = SyncEngine(garmin_service)
scheduler = SyncScheduler(engine)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Garmin → COROS Sync", lifespan=lifespan)


# ------------------------------------------------------------------ auth
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if APP_PASSWORD and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, APP_PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Garmin COROS Sync"'})
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness probe for Docker; also confirms the scheduler thread is alive."""
    if not scheduler.scheduler.running:
        raise HTTPException(status_code=503, detail="scheduler not running")
    return {"ok": True}


# ---------------------------------------------------------------- models
class Credentials(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


class MfaCode(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class SyncRequest(BaseModel):
    lookback_days: int | None = Field(default=None, ge=1, le=MAX_LOOKBACK_DAYS)


class Settings(BaseModel):
    lookback_days: int = Field(ge=1, le=MAX_LOOKBACK_DAYS)


class Schedule(BaseModel):
    enabled: bool
    mode: str
    interval_hours: int
    time: str
    days: list[str]


def _err(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


# ----------------------------------------------------------------- state
@app.get("/api/state")
def state():
    g = db.get_setting("garmin_credentials") or {}
    c = db.get_setting("coros_credentials") or {}
    cs = db.get_setting("coros_session") or {}
    return {
        "garmin": {
            "connected": bool(g) and garmin_service.has_tokens(),
            "email": g.get("email"),
            "display_name": g.get("display_name"),
            "mfa_pending": garmin_service.mfa_pending,
            "status": db.get_setting("garmin_status"),
        },
        "coros": {
            "connected": bool(c),
            "email": c.get("email"),
            "nickname": cs.get("nickname"),
            "region": cs.get("region"),
            "status": db.get_setting("coros_status"),
        },
        "schedule": {**scheduler.get(), "next_run": scheduler.next_run(), "interval_choices": INTERVAL_CHOICES},
        "settings": {"lookback_days": get_lookback_days(), "max_lookback_days": MAX_LOOKBACK_DAYS},
        "sync": engine.progress,
        "last_run": db.last_run(),
        "last_success": db.last_successful_run(),
        "totals": db.totals(),
    }


# ---------------------------------------------------------------- garmin
@app.post("/api/garmin/connect")
async def garmin_connect(body: Credentials):
    try:
        result = await run_in_threadpool(garmin_service.start_connect, body.email.strip(), body.password)
    except GarminAuthError as exc:
        raise _err(400, str(exc)) from exc
    # Credentials are kept so unattended syncs can re-login if the refresh token lapses.
    db.set_setting(
        "garmin_credentials",
        {"email": body.email.strip(), "password": body.password, "display_name": result.display_name},
    )
    if result.status == "connected":
        db.set_setting("garmin_status", {"ok": True, "error": None, "at": db.now_iso()})
    return {"status": result.status, "display_name": result.display_name}


@app.post("/api/garmin/mfa")
async def garmin_mfa(body: MfaCode):
    try:
        result = await run_in_threadpool(garmin_service.submit_mfa, body.code)
    except GarminAuthError as exc:
        raise _err(400, str(exc)) from exc
    creds = db.get_setting("garmin_credentials") or {}
    creds["display_name"] = result.display_name
    db.set_setting("garmin_credentials", creds)
    db.set_setting("garmin_status", {"ok": True, "error": None, "at": db.now_iso()})
    return {"status": result.status, "display_name": result.display_name}


@app.post("/api/garmin/disconnect")
def garmin_disconnect():
    garmin_service.disconnect()
    db.delete_setting("garmin_credentials")
    db.delete_setting("garmin_status")
    return {"ok": True}


# ----------------------------------------------------------------- coros
@app.post("/api/coros/connect")
async def coros_connect(body: Credentials):
    client = CorosClient(body.email.strip(), body.password)
    try:
        session = await run_in_threadpool(client.login)
    except CorosError as exc:
        raise _err(400, str(exc)) from exc
    finally:
        client.close()
    db.set_setting("coros_credentials", {"email": body.email.strip(), "password": body.password})
    db.set_setting("coros_session", session.to_dict())
    db.set_setting("coros_status", {"ok": True, "error": None, "at": db.now_iso()})
    return {"status": "connected", "nickname": session.nickname, "region": session.region}


@app.post("/api/coros/disconnect")
def coros_disconnect():
    for key in ("coros_credentials", "coros_session", "coros_status"):
        db.delete_setting(key)
    return {"ok": True}


# ------------------------------------------------------------------ sync
@app.post("/api/sync", status_code=202)
def sync_now(body: SyncRequest | None = None):
    try:
        run_id = engine.start("manual", body.lookback_days if body else None)
    except SyncAlreadyRunning as exc:
        raise _err(409, str(exc)) from exc
    except NotConfigured as exc:
        raise _err(400, str(exc)) from exc
    return {"run_id": run_id}


@app.put("/api/schedule")
def update_schedule(body: Schedule):
    try:
        sched = scheduler.update(body.model_dump())
    except ValueError as exc:
        raise _err(400, str(exc)) from exc
    return {**sched, "next_run": scheduler.next_run()}


@app.put("/api/settings")
def update_settings(body: Settings):
    db.set_setting("lookback_days", body.lookback_days)
    return {"lookback_days": body.lookback_days}


# --------------------------------------------------------------- history
@app.get("/api/runs")
def runs(limit: int = 25, offset: int = 0):
    items, total = db.list_runs(max(1, min(limit, 200)), max(0, offset))
    return {"runs": items, "total": total}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise _err(404, "Run not found")
    return run


@app.delete("/api/runs")
def clear_runs():
    if engine.is_running():
        raise _err(409, "Wait for the current sync to finish.")
    db.clear_history()
    return {"ok": True}


@app.post("/api/activities/{activity_id}/resync")
def resync_activity(activity_id: str):
    """Forget that an activity was synced so the next run uploads it again."""
    db.unmark_synced(activity_id)
    return {"ok": True}


# ---------------------------------------------------------------- static
@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
