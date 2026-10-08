#!/usr/bin/env python3
"""Paced async HTTP load driver. Requires httpx (included in the backend image)."""

import argparse
import asyncio
import math
import statistics
import time
from pathlib import Path

import httpx


async def send_one(client, url, payload, timeout, scheduled_at, run_started):
    started = time.perf_counter()
    try:
        response = await client.post(
            url,
            content=payload,
            headers={"Content-Type": "text/xml; charset=ISO-8859-1", "Accept": "text/xml"},
            timeout=timeout,
        )
        server_elapsed = response.headers.get("X-Simulator-Elapsed-Ms")
        return {
            "status": response.status_code,
            "error": None,
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


def percentile(values: list[float], percent: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((percent / 100) * len(ordered)) - 1)
    return ordered[index]


async def run(args) -> int:
    payload_path = Path(args.file)
    if not payload_path.is_file():
        raise SystemExit(f"Request file not found: {payload_path}")
    payload = payload_path.read_bytes()
    total = math.ceil(args.tps * args.duration)
    # Keep a bounded number of in-flight requests. HTTP keep-alive avoids a new
    # TCP connection and thread for every request.
    concurrency = args.concurrency
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(args.timeout, connect=min(args.timeout, 5.0))
    results = []
    pending: set[asyncio.Task] = set()
    skipped = 0
    run_started = time.perf_counter()

    print(f"Target: {args.tps:g} TPS for {args.duration:g}s ({total} scheduled requests)")
    print(f"URL: {args.url}")
    print(f"Async connection limit: {concurrency}; client uses HTTP keep-alive")

    async with httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False) as client:
        for sequence in range(total):
            scheduled_at = run_started + sequence / args.tps
            delay = scheduled_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

            completed = {task for task in pending if task.done()}
            for task in completed:
                results.append(task.result())
            pending.difference_update(completed)

            if len(pending) >= concurrency:
                skipped += 1
                continue
            pending.add(asyncio.create_task(send_one(client, args.url, payload, timeout, scheduled_at, run_started)))

        if pending:
            results.extend(await asyncio.gather(*pending))

    elapsed = time.perf_counter() - run_started
    latencies = [item["latency_ms"] for item in results]
    schedule_lags = [item["schedule_lag_ms"] for item in results]
    server_latencies = [item["server_elapsed_ms"] for item in results if item["server_elapsed_ms"] is not None]
    counts: dict[str, int] = {}
    errors = []
    failed = 0
    for item in results:
        label = str(item["status"]) if item["status"] is not None else "network_error"
        counts[label] = counts.get(label, 0) + 1
        if item["status"] is None or not 200 <= item["status"] < 300:
            failed += 1
        if item["error"] and len(errors) < 5:
            errors.append(item["error"])

    starts = sorted(item["start_offset"] for item in results)
    actual_rate = (len(starts) - 1) / (starts[-1] - starts[0]) if len(starts) > 1 and starts[-1] > starts[0] else 0.0
    print("\nResults")
    print(f"  completed: {len(results)} / {total}")
    print(f"  skipped at client concurrency limit: {skipped}")
    print(f"  non-2xx/network failures: {failed}")
    print(f"  wall time including final responses: {elapsed:.2f}s")
    print(f"  observed request start rate: {actual_rate:.1f} TPS")
    print(f"  HTTP/network outcomes: {counts}")
    if latencies:
        print(f"  latency ms: p50={percentile(latencies, 50):.1f}, p95={percentile(latencies, 95):.1f}, p99={percentile(latencies, 99):.1f}, mean={statistics.mean(latencies):.1f}")
    else:
        print("  latency ms: no completed requests")
    print(f"  schedule lag ms: p95={percentile(schedule_lags, 95):.1f}, max={max(schedule_lags, default=0):.1f}")
    if server_latencies:
        print(f"  server processing ms: p50={percentile(server_latencies, 50):.1f}, p95={percentile(server_latencies, 95):.1f}, p99={percentile(server_latencies, 99):.1f}")
    for error in errors:
        print(f"  error: {error}")
    return 0 if results and skipped == 0 and failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Send paced SOAP/XML load to a simulator scenario.")
    parser.add_argument("--url", default="http://bss-simulator-api:8000/sim/Hlr-prov", help="Simulator scenario URL")
    parser.add_argument("--file", default="test.xml", help="SOAP/XML request fixture")
    parser.add_argument("--tps", type=float, default=200, help="Target request starts per second")
    parser.add_argument("--duration", type=float, default=30, help="Load duration in seconds")
    parser.add_argument("--timeout", type=float, default=10, help="Per-request timeout in seconds")
    parser.add_argument("--concurrency", type=int, default=500, help="Maximum in-flight requests")
    args = parser.parse_args()
    if args.tps <= 0 or args.duration <= 0 or args.timeout <= 0 or args.concurrency <= 0:
        parser.error("--tps, --duration, --timeout, and --concurrency must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
