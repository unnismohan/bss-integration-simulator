import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import psutil
from sqlalchemy import case, select, update

from .async_db import AsyncSessionLocal
from .config import CALLBACK_ALLOWED_HOSTS, CALLBACK_BATCH_SIZE, CALLBACK_POLL_SECONDS, CALLBACK_TIMEOUT_SECONDS
from .models import CallbackJob, CallbackWorkerControl, ServiceMetric


def allowed_callback_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname is not None and parsed.hostname.lower() in CALLBACK_ALLOWED_HOSTS


async def deliver_batch(client: httpx.AsyncClient) -> int:
    """Claim and deliver up to CALLBACK_BATCH_SIZE jobs; return the claimed count."""
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=max(30, CALLBACK_TIMEOUT_SECONDS * 3))
        async with db.begin():
            control = await db.get(CallbackWorkerControl, 1)
            if control is None:
                control = CallbackWorkerControl(id=1, enabled=True)
                db.add(control)
            elif not control.enabled:
                return -1
            # Recover work left IN_PROGRESS if a worker was interrupted.
            await db.execute(
                update(CallbackJob)
                .where(CallbackJob.state == "IN_PROGRESS", CallbackJob.claimed_at < stale_before)
                .values(state="RETRY_WAIT", claimed_at=None, due_at=now)
            )
            jobs = list((await db.scalars(
                select(CallbackJob)
                .where(CallbackJob.state.in_(["PENDING", "RETRY_WAIT"]), CallbackJob.due_at <= now)
                .order_by(CallbackJob.due_at, CallbackJob.id)
                .limit(CALLBACK_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )).all())
            for job in jobs:
                job.state = "IN_PROGRESS"
                job.attempts += 1
                job.claimed_at = now
            snapshots = [
                {
                    "id": job.id,
                    "url": job.callback_url,
                    "payload": job.payload,
                    "content_type": job.content_type,
                    "correlation_id": job.correlation_id,
                    "attempts": job.attempts,
                    "max_attempts": job.max_attempts,
                    "retry_delay_ms": job.retry_delay_ms,
                }
                for job in jobs
            ]

    if not snapshots:
        return 0

    async def send(item):
        try:
            if not allowed_callback_url(item["url"]):
                raise ValueError("callback URL host is not in CALLBACK_ALLOWED_HOSTS")
            response = await client.post(
                item["url"],
                content=item["payload"].encode("utf-8"),
                headers={"Content-Type": item["content_type"], "X-Correlation-ID": item["correlation_id"]},
            )
            response.raise_for_status()
            return item, None
        except Exception as exc:
            detail = str(exc).strip()
            error = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            return item, error[:1000]

    results = await asyncio.gather(*(send(item) for item in snapshots))
    succeeded = []
    retry_errors = []
    final_errors = []
    for item, error in results:
        if error is None:
            succeeded.append(item["id"])
        elif item["attempts"] < item["max_attempts"]:
            retry_errors.append((item, error))
        else:
            final_errors.append((item, error))

    async with AsyncSessionLocal() as db:
        async with db.begin():
            # Update each outcome class in bulk. Avoid one SELECT per completed job.
            if succeeded:
                await db.execute(
                    update(CallbackJob)
                    .where(CallbackJob.id.in_(succeeded), CallbackJob.state == "IN_PROGRESS")
                    .values(state="SUCCEEDED", claimed_at=None, last_error=None)
                )
            for failed, state in ((retry_errors, "RETRY_WAIT"), (final_errors, "FAILED")):
                if not failed:
                    continue
                error_by_id = {item["id"]: error for item, error in failed}
                values = {
                    "state": state,
                    "claimed_at": None,
                    "last_error": case(error_by_id, value=CallbackJob.id, else_=None),
                }
                if state == "RETRY_WAIT":
                    now = datetime.now(timezone.utc)
                    due_by_id = {
                        item["id"]: now + timedelta(milliseconds=item["retry_delay_ms"])
                        for item, _ in failed
                    }
                    values["due_at"] = case(due_by_id, value=CallbackJob.id, else_=now)
                await db.execute(
                    update(CallbackJob)
                    .where(CallbackJob.id.in_(list(error_by_id)), CallbackJob.state == "IN_PROGRESS")
                    .values(**values)
                )
    return len(snapshots)


async def update_worker_metrics(process: psutil.Process):
    async with AsyncSessionLocal() as db:
        async with db.begin():
            metric = await db.get(ServiceMetric, "callback-worker")
            if metric is None:
                metric = ServiceMetric(service_name="callback-worker")
                db.add(metric)
            metric.cpu_percent = process.cpu_percent(interval=None)
            metric.memory_bytes = process.memory_info().rss
            metric.updated_at = datetime.now(timezone.utc)


async def main():
    print("Callback worker started")
    process = psutil.Process()
    process.cpu_percent(interval=None)
    last_metrics_at = 0.0
    limits = httpx.Limits(
        max_connections=max(1, CALLBACK_BATCH_SIZE),
        max_keepalive_connections=max(1, CALLBACK_BATCH_SIZE),
    )
    timeout = httpx.Timeout(CALLBACK_TIMEOUT_SECONDS)
    # Keep connections warm across batches; recreating the client per batch discarded TCP/TLS keep-alive.
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False) as client:
        while True:
            try:
                claimed = await deliver_batch(client)
            except Exception as exc:
                print(f"Callback worker batch failed: {exc}")
                claimed = 0
            loop_now = asyncio.get_running_loop().time()
            if loop_now - last_metrics_at >= 2.0:
                try:
                    await update_worker_metrics(process)
                    last_metrics_at = loop_now
                except Exception as exc:
                    print(f"Callback worker metrics update failed: {exc}")
            # Drain continuously while work is available; poll only when the queue is empty.
            if claimed <= 0:
                await asyncio.sleep(CALLBACK_POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
