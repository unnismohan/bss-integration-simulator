#!/usr/bin/env python3
"""Paced async load driver for the OCS subscriber lookup GET scenario.

Requires httpx (included in the simulator backend image):
    python3 -m pip install httpx

The URL must contain the {msisdn} placeholder. Each request gets a unique,
random numeric MSISDN and a unique X-Correlation-Id.
"""

import argparse
import asyncio
import math
import os
import secrets
import statistics
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx


def percentile(values: list[float], percent: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((percent / 100) * len(ordered)) - 1)
    return ordered[index]


def make_msisdn(prefix: str, subscriber_digits: int, generated: set[str]) -> str:
    while True:
        tail = f"{secrets.randbelow(10 ** subscriber_digits):0{subscriber_digits}d}"
        msisdn = prefix + tail
        if msisdn not in generated:
            generated.add(msisdn)
            return msisdn


async def run(args) -> int:
    total = math.ceil(args.tps * args.duration)
    timeout = httpx.Timeout(args.timeout, connect=min(args.timeout, 5.0))
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    results: list[dict] = []
    pending: set[asyncio.Task] = set()
    skipped = 0
    generated: set[str] = set()
    run_started = time.perf_counter()
    date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")

    print(f"Target: {args.tps:g} TPS for {args.duration:g}s ({total} scheduled requests)")
    print(f"URL template: {args.url}")
    print(f"Concurrency limit: {args.concurrency}; HTTP keep-alive enabled")
    print(f"Random unique MSISDNs use prefix {args.prefix!r} plus {args.subscriber_digits} digits")

    async with httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False) as client:
        async def send_one(sequence: int, scheduled_at: float):
            msisdn = make_msisdn(args.prefix, args.subscriber_digits, generated)
            url = args.url.replace("{msisdn}", quote(msisdn, safe=""))
            correlation_id = f"OCS-REQ-{date_prefix}-{sequence:08d}-{uuid.uuid4().hex[:8].upper()}"
            headers = {
                "Accept": "application/json",
                "X-Correlation-Id": correlation_id,
            }
            api_key = os.environ.get("SIMULATOR_API_KEY", "")
            if api_key:
                headers["X-Simulator-Api-Key"] = api_key
            started = time.perf_counter()
            try:
                response = await client.get(url, headers=headers)
                server_elapsed = response.headers.get("X-Simulator-Elapsed-Ms")
                error = f"HTTP {response.status_code}: {response.text[:300]}" if response.status_code >= 400 else None
                return {
                    "status": response.status_code,
                    "error": error,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "start_offset": started - run_started,
                    "schedule_lag_ms": max(0.0, (started - scheduled_at) * 1000),
                    "server_elapsed_ms": float(server_elapsed) if server_elapsed else None,
                }
            except httpx.HTTPError as exc:
                return {
                    "status": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "start_offset": started - run_started,
                    "schedule_lag_ms": max(0.0, (started - scheduled_at) * 1000),
                    "server_elapsed_ms": None,
                }

        for sequence in range(1, total + 1):
            scheduled_at = run_started + (sequence - 1) / args.tps
            delay = scheduled_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

            completed = {task for task in pending if task.done()}
            for task in completed:
                results.append(task.result())
            pending.difference_update(completed)

            if len(pending) >= args.concurrency:
                skipped += 1
                continue
            pending.add(asyncio.create_task(send_one(sequence, scheduled_at)))

        if pending:
            results.extend(await asyncio.gather(*pending))

    elapsed = time.perf_counter() - run_started
    latencies = [item["latency_ms"] for item in results]
    schedule_lags = [item["schedule_lag_ms"] for item in results]
    server_latencies = [item["server_elapsed_ms"] for item in results if item["server_elapsed_ms"] is not None]
    outcomes: dict[str, int] = {}
    failures = 0
    errors: list[str] = []
    for item in results:
        status = str(item["status"]) if item["status"] is not None else "network_error"
        outcomes[status] = outcomes.get(status, 0) + 1
        if item["status"] is None or not 200 <= item["status"] < 300:
            failures += 1
        if item["error"] and len(errors) < 5:
            errors.append(item["error"])

    starts = sorted(item["start_offset"] for item in results)
    actual_tps = (len(starts) - 1) / (starts[-1] - starts[0]) if len(starts) > 1 and starts[-1] > starts[0] else 0.0
    print("\nResults")
    print(f"  completed: {len(results)} / {total}")
    print(f"  skipped at client concurrency limit: {skipped}")
    print(f"  non-2xx/network failures: {failures}")
    print(f"  wall time including final responses: {elapsed:.2f}s")
    print(f"  observed request start rate: {actual_tps:.1f} TPS")
    print(f"  HTTP outcomes: {outcomes}")
    if latencies:
        print(f"  latency ms: p50={percentile(latencies, 50):.1f}, p95={percentile(latencies, 95):.1f}, p99={percentile(latencies, 99):.1f}, mean={statistics.mean(latencies):.1f}")
    print(f"  schedule lag ms: p95={percentile(schedule_lags, 95):.1f}, max={max(schedule_lags, default=0):.1f}")
    if server_latencies:
        print(f"  server processing ms: p50={percentile(server_latencies, 50):.1f}, p95={percentile(server_latencies, 95):.1f}, p99={percentile(server_latencies, 99):.1f}")
    for error in errors:
        print(f"  error: {error}")
    return 0 if results and skipped == 0 and failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test the OCS subscriber lookup GET scenario")
    parser.add_argument("--url", default="http://localhost:8000/sim/ocs/subscriber/{msisdn}", help="GET URL template; include {msisdn}")
    parser.add_argument("--tps", type=float, default=200, help="Target request starts per second")
    parser.add_argument("--duration", type=float, default=30, help="Load duration in seconds")
    parser.add_argument("--timeout", type=float, default=10, help="Per-request timeout in seconds")
    parser.add_argument("--concurrency", type=int, default=500, help="Maximum in-flight requests")
    parser.add_argument("--prefix", default="2438", help="Numeric prefix for generated MSISDNs")
    parser.add_argument("--subscriber-digits", type=int, default=8, help="Random digits appended to the prefix")
    args = parser.parse_args()
    if args.tps <= 0 or args.duration <= 0 or args.timeout <= 0 or args.concurrency <= 0:
        parser.error("--tps, --duration, --timeout, and --concurrency must be positive")
    if "{msisdn}" not in args.url:
        parser.error("--url must contain the {msisdn} placeholder")
    if not args.prefix.isdigit():
        parser.error("--prefix must contain only digits")
    if args.subscriber_digits < 1 or args.subscriber_digits > 12:
        parser.error("--subscriber-digits must be between 1 and 12")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
