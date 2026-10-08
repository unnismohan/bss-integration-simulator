import asyncio
import json
import os
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from urllib.parse import urlparse

import psutil
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.orm import Session

from .async_db import AsyncSessionLocal, engine as async_engine
from .config import APP_ENV, CALLBACK_ALLOWED_HOSTS, CORS_ORIGINS, ENABLE_API_DOCS, MAX_REQUEST_BYTES, SIMULATOR_API_KEY
from .db import Base, engine, get_db
from .models import CallbackJob, CallbackWorkerControl, Scenario, ScenarioVersion, ServiceMetric
from .renderer import capture_values, render, validate_captures
from .schemas import ScenarioDefinition

if APP_ENV == "production" and len(SIMULATOR_API_KEY) < 32:
    raise RuntimeError("SIMULATOR_API_KEY must be set to at least 32 characters in production")

Base.metadata.create_all(bind=engine)
app = FastAPI(
    title="BSS Integration Simulator",
    version="0.1.0",
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])
_metric_process = psutil.Process(os.getpid())
_metric_process.cpu_percent(interval=None)
_simulator_requests = deque(maxlen=100_000)
_simulator_requests_lock = threading.Lock()
_simulator_started_at = time.time()
_simulator_request_totals = {"requests": 0, "successful": 0, "failed": 0}
_scenario_cache: OrderedDict[str, tuple[float, int, ScenarioDefinition]] = OrderedDict()
_scenario_cache_lock = threading.Lock()
_scenario_cache_load_locks: dict[str, asyncio.Lock] = {}
_scenario_cache_ttl_seconds = 2.0
_scenario_cache_max_entries = 512


def invalidate_scenario_cache(key: str) -> None:
    with _scenario_cache_lock:
        _scenario_cache.pop(key, None)


async def load_scenario_for_simulation(key: str) -> tuple[int, ScenarioDefinition] | None:
    now = time.monotonic()
    with _scenario_cache_lock:
        cached = _scenario_cache.get(key)
        if cached is not None:
            cached_at, version, definition = cached
            if now - cached_at < _scenario_cache_ttl_seconds:
                _scenario_cache.move_to_end(key)
                return version, definition
            _scenario_cache.pop(key, None)

    # Collapse concurrent cold/expired lookups for the same key without
    # serializing cache misses for unrelated scenarios.
    with _scenario_cache_lock:
        load_lock = _scenario_cache_load_locks.setdefault(key, asyncio.Lock())
    async with load_lock:
        now = time.monotonic()
        with _scenario_cache_lock:
            cached = _scenario_cache.get(key)
            if cached is not None:
                cached_at, version, definition = cached
                if now - cached_at < _scenario_cache_ttl_seconds:
                    _scenario_cache.move_to_end(key)
                    return version, definition
                _scenario_cache.pop(key, None)

        async with AsyncSessionLocal() as db:
            row = (await db.scalars(select(Scenario).where(Scenario.key == key))).one_or_none()
            if row is None:
                return None
            definition_json = row.definition
            version = row.version

        definition = ScenarioDefinition.model_validate_json(definition_json)
        with _scenario_cache_lock:
            _scenario_cache[key] = (time.monotonic(), version, definition)
            _scenario_cache.move_to_end(key)
            while len(_scenario_cache) > _scenario_cache_max_entries:
                _scenario_cache.popitem(last=False)
        return version, definition


@app.on_event("shutdown")
async def close_async_database_pool():
    await async_engine.dispose()
    engine.dispose()


@app.middleware("http")
async def require_api_key_in_production(request: Request, call_next):
    if APP_ENV == "production" and request.url.path not in {"/health/live", "/health/ready"} and request.method != "OPTIONS":
        supplied = request.headers.get("X-Simulator-Api-Key", "")
        if not compare_digest(supplied, SIMULATOR_API_KEY):
            return JSONResponse(status_code=401, content={"detail": "A valid simulator API key is required"})
    return await call_next(request)


@app.middleware("http")
async def track_simulator_throughput(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/sim/"):
        now = time.time()
        successful = 200 <= response.status_code < 400
        with _simulator_requests_lock:
            _simulator_requests.append((now, successful))
            _simulator_request_totals["requests"] += 1
            _simulator_request_totals["successful" if successful else "failed"] += 1
    return response


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ready"}


@app.get("/api/metrics")
def simulator_metrics(db: Session = Depends(get_db)):
    """Live throughput, process resources, and async callback queue snapshot."""
    now = time.time()
    window_seconds = 10
    cutoff = now - window_seconds
    with _simulator_requests_lock:
        while _simulator_requests and _simulator_requests[0][0] < cutoff:
            _simulator_requests.popleft()
        recent = list(_simulator_requests)
        totals = dict(_simulator_request_totals)
    measurement_window = min(window_seconds, max(0.001, now - _simulator_started_at))
    successful_recent = sum(1 for _, successful in recent if successful)
    failed_recent = len(recent) - successful_recent

    rows = db.execute(
        select(CallbackJob.state, func.count(CallbackJob.id)).group_by(CallbackJob.state)
    )
    counts = {state: count for state, count in rows}
    failure_reason = case(
        (CallbackJob.last_error.ilike("%callback URL host is not in%"), "Callback host is not allowed"),
        (CallbackJob.last_error.ilike("%ConnectTimeout%"), "Connection timed out"),
        (CallbackJob.last_error.ilike("%ReadTimeout%"), "Callback response timed out"),
        (CallbackJob.last_error.ilike("%ConnectError%"), "Could not connect to callback endpoint"),
        (CallbackJob.last_error.ilike("%ReadError%"), "Callback connection was interrupted"),
        (CallbackJob.last_error.ilike("%WriteTimeout%"), "Callback request timed out while sending"),
        (CallbackJob.last_error.ilike("%PoolTimeout%"), "Worker HTTP connection pool timed out"),
        (CallbackJob.last_error.ilike("%RemoteProtocolError%"), "Callback endpoint closed or malformed the connection"),
        (CallbackJob.last_error.ilike("%Server error%"), "Callback endpoint returned HTTP 5xx"),
        (CallbackJob.last_error.ilike("%Client error%"), "Callback endpoint returned HTTP 4xx"),
        (CallbackJob.last_error.is_(None) | (CallbackJob.last_error == ""), "No error details recorded"),
        else_=func.left(CallbackJob.last_error, 180),
    )
    failure_rows = db.execute(
        select(failure_reason.label("reason"), func.count(CallbackJob.id).label("count"))
        .where(CallbackJob.state == "FAILED")
        .group_by(failure_reason)
        .order_by(func.count(CallbackJob.id).desc())
        .limit(10)
    )
    failure_reasons = [{"reason": reason, "count": count} for reason, count in failure_rows]
    queued = counts.get("PENDING", 0) + counts.get("RETRY_WAIT", 0)
    api_memory = _metric_process.memory_info().rss
    api_cpu = _metric_process.cpu_percent(interval=None)
    worker = db.get(ServiceMetric, "callback-worker")
    worker_control = db.get(CallbackWorkerControl, 1)
    worker_metrics = None
    if worker is not None:
        worker_metrics = {
            "cpu_percent": round(worker.cpu_percent, 2),
            "memory_bytes": worker.memory_bytes,
            "memory_mb": round(worker.memory_bytes / (1024 * 1024), 2),
            "updated_at": worker.updated_at.isoformat(),
        }
    return {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "throughput": {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "successful": successful_recent,
            "failed": failed_recent,
            "request_tps": round(len(recent) / measurement_window, 2),
            "successful_tps": round(successful_recent / measurement_window, 2),
            "total_requests": totals["requests"],
            "total_successful": totals["successful"],
            "total_failed": totals["failed"],
        },
        "resources": {
            "api": {
                "cpu_percent": round(api_cpu, 2),
                "memory_bytes": api_memory,
                "memory_mb": round(api_memory / (1024 * 1024), 2),
            },
            "callback_worker": worker_metrics,
        },
        "callback_worker": {"enabled": worker_control.enabled if worker_control else True},
        "callback_jobs": {
            "pending": counts.get("PENDING", 0),
            "retry_wait": counts.get("RETRY_WAIT", 0),
            "in_progress": counts.get("IN_PROGRESS", 0),
            "succeeded": counts.get("SUCCEEDED", 0),
            "failed": counts.get("FAILED", 0),
            "queue_depth": queued,
            "failure_reasons": failure_reasons,
        }
    }


@app.post("/api/callback-worker/stop-and-clear")
def stop_and_clear_callback_jobs(db: Session = Depends(get_db)):
    """Pause callback delivery and clear queued jobs plus delivery history."""
    control = db.get(CallbackWorkerControl, 1)
    if control is None:
        control = CallbackWorkerControl(id=1, enabled=False)
        db.add(control)
    else:
        control.enabled = False
    deleted = db.execute(delete(CallbackJob)).rowcount or 0
    db.commit()
    return {
        "enabled": False,
        "deleted_jobs": deleted,
        "message": "Callback worker paused and callback jobs cleared. A callback already in flight may finish sending.",
    }


@app.post("/api/callback-worker/resume")
def resume_callback_worker(db: Session = Depends(get_db)):
    control = db.get(CallbackWorkerControl, 1)
    if control is None:
        control = CallbackWorkerControl(id=1, enabled=True)
        db.add(control)
    else:
        control.enabled = True
    db.commit()
    return {"enabled": True, "message": "Callback worker resumed."}


@app.get("/api/scenarios")
def list_scenarios(
    search: str = Query("", max_length=100),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Return compact, searchable scenario summaries for the builder sidebar."""
    filters = []
    term = search.strip()
    if term:
        escaped = term.replace("/", "//").replace("%", "/%").replace("_", "/_")
        filters.append(
            or_(Scenario.key.ilike(f"%{escaped}%", escape="/"), Scenario.name.ilike(f"%{escaped}%", escape="/"))
        )
    total = db.scalar(select(func.count(Scenario.id)).where(*filters)) or 0
    rows = db.execute(
        select(Scenario.key, Scenario.name, Scenario.version, Scenario.definition)
        .where(*filters)
        .order_by(Scenario.key)
        .offset(offset)
        .limit(limit)
    )
    items = [
        {"key": key, "name": name, "version": version, "flow": json.loads(definition).get("flow", "sync")}
        for key, name, version, definition in rows
    ]
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@app.get("/api/scenarios/{key}")
def get_scenario(key: str, db: Session = Depends(get_db)):
    row = db.scalar(select(Scenario).where(Scenario.key == key))
    if row is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return {"key": row.key, "name": row.name, "version": row.version, "definition": json.loads(row.definition)}


@app.post("/api/scenarios/validate")
def validate_scenario(definition: ScenarioDefinition):
    try:
        validate_captures(definition)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"valid": True, "key": definition.key}


@app.get("/api/scenarios/{key}/versions")
def list_scenario_versions(key: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(ScenarioVersion).where(ScenarioVersion.scenario_key == key).order_by(ScenarioVersion.version.desc()))
    return [{"version": row.version, "created_at": row.created_at} for row in rows]


@app.get("/api/scenarios/{key}/versions/{version}")
def get_scenario_version(key: str, version: int, db: Session = Depends(get_db)):
    row = db.scalar(select(ScenarioVersion).where(ScenarioVersion.scenario_key == key, ScenarioVersion.version == version))
    if row is None:
        raise HTTPException(status_code=404, detail="Scenario version not found")
    return {"key": key, "version": version, "definition": json.loads(row.definition)}


@app.put("/api/scenarios/{key}")
def save_scenario(key: str, definition: ScenarioDefinition, db: Session = Depends(get_db)):
    if key != definition.key:
        raise HTTPException(status_code=400, detail="URL key must match definition key")
    try:
        validate_captures(definition)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = db.scalar(select(Scenario).where(Scenario.key == key))
    if row is None:
        row = Scenario(key=key, name=definition.name, version=1, definition=definition.model_dump_json())
        db.add(row)
        db.flush()
    else:
        row.version += 1
        row.name = definition.name
        row.definition = definition.model_dump_json()
    db.add(ScenarioVersion(scenario_key=key, version=row.version, definition=definition.model_dump_json()))
    db.commit()
    invalidate_scenario_cache(key)
    return {"key": row.key, "name": row.name, "version": row.version}


@app.delete("/api/scenarios/{key}", status_code=204)
def delete_scenario(key: str, db: Session = Depends(get_db)):
    row = db.scalar(select(Scenario).where(Scenario.key == key))
    if row is not None:
        db.delete(row)
        db.commit()
        invalidate_scenario_cache(key)
    return Response(status_code=204)


@app.get("/api/callback-jobs")
def callback_jobs(db: Session = Depends(get_db)):
    rows = db.scalars(select(CallbackJob).order_by(CallbackJob.created_at.desc()).limit(100))
    return [{"id": r.id, "scenario_key": r.scenario_key, "scenario_version": r.scenario_version, "correlation_id": r.correlation_id, "state": r.state, "attempts": r.attempts, "due_at": r.due_at, "last_error": r.last_error} for r in rows]


def match_path_template(template: str, request_path: str | None) -> dict[str, str] | None:
    expected = template.strip("/").split("/") if template.strip("/") else []
    actual = request_path.strip("/").split("/") if request_path and request_path.strip("/") else []
    if len(expected) != len(actual):
        return None
    params: dict[str, str] = {}
    for expected_part, actual_part in zip(expected, actual):
        if expected_part.startswith("{") and expected_part.endswith("}"):
            if not actual_part:
                return None
            params[expected_part[1:-1]] = actual_part
        elif expected_part != actual_part:
            return None
    return params


@app.api_route("/sim/{key}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def simulate(key: str, request: Request):
    return await simulate_path(key, None, request)


@app.api_route("/sim/{key}/{request_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def simulate_with_path(key: str, request_path: str, request: Request):
    return await simulate_path(key, request_path, request)


async def simulate_path(key: str, request_path: str | None, request: Request):
    started = time.perf_counter()
    scenario_snapshot = await load_scenario_for_simulation(key)
    if scenario_snapshot is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    scenario_version, scenario = scenario_snapshot
    if request.method.upper() != scenario.method.upper():
        raise HTTPException(status_code=405, detail="Method does not match configured scenario")
    path_params = match_path_template(scenario.path_template, request_path)
    if path_params is None:
        expected_path = f"/sim/{key}/{scenario.path_template.strip('/')}" if scenario.path_template else f"/sim/{key}"
        raise HTTPException(status_code=404, detail=f"Request path does not match this scenario. Expected {expected_path}")
    content_type = request.headers.get("content-type", "").lower()
    expected_type = "json" if scenario.request_format == "json" else "xml"
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="Request body exceeds configured maximum")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
    body_chunks = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Request body exceeds configured maximum")
        body_chunks.append(chunk)
    body = b"".join(body_chunks)
    if body_size > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body exceeds configured maximum")
    if expected_type not in content_type and (request.method.upper() != "GET" or body_size > 0 or bool(content_type)):
        raise HTTPException(status_code=415, detail=f"Scenario expects an {expected_type.upper()} request")
    captures = capture_values(scenario, body, dict(request.headers), dict(request.query_params), path_params)
    if scenario.flow == "sync":
        data, content_type, status, headers = render(scenario.response, captures, scenario.response_format)
    else:
        config = scenario.async_config
        parsed_callback = urlparse(config.callback_url)
        if parsed_callback.scheme not in {"http", "https"} or not parsed_callback.hostname or parsed_callback.hostname.lower() not in CALLBACK_ALLOWED_HOSTS:
            raise HTTPException(status_code=422, detail="Callback URL host is not in CALLBACK_ALLOWED_HOSTS")
        correlation = str(captures[config.correlation_capture])
        callback = scenario.callback
        ack_data, ack_content_type, ack_status, ack_headers = render(scenario.ack, captures, scenario.ack_format)
        data, callback_content_type, _, _ = render(callback, captures, scenario.callback_format)
        job = CallbackJob(
            scenario_key=key,
            scenario_version=scenario_version,
            correlation_id=correlation,
            callback_url=config.callback_url,
            content_type=callback_content_type,
            payload=data.decode("utf-8"),
            due_at=datetime.now(timezone.utc) + timedelta(milliseconds=config.delay_ms),
            state="PENDING",
            attempts=0,
            max_attempts=config.max_attempts,
            retry_delay_ms=config.retry_delay_ms,
        )
        async with AsyncSessionLocal() as db:
            db.add(job)
            await db.commit()
        data, content_type, status, headers = ack_data, ack_content_type, ack_status, ack_headers
    headers = dict(headers)
    headers["X-Simulator-Scenario"] = key
    headers["X-Simulator-Version"] = str(scenario_version)
    headers["X-Simulator-Elapsed-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
    if scenario.flow == "async":
        headers["X-Correlation-ID"] = correlation
    return Response(content=data, status_code=status, media_type=content_type, headers=headers)
