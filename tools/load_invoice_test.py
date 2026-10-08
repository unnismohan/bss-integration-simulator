#!/usr/bin/env python3
"""Paced async load driver for the ERP invoice async scenario.

Requires httpx: python3 -m pip install httpx
Each scheduled request gets unique request/customer/account/item identifiers.
"""

import argparse
import asyncio
import math
import statistics
import time
import uuid
from datetime import date

import httpx


def percentile(values: list[float], percent: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((percent / 100) * len(ordered)) - 1)
    return ordered[index]


def make_invoice(sequence: int) -> tuple[str, dict, dict[str, str]]:
    unique = uuid.uuid4().hex[:12].upper()
    today = date.today()
    request_id = f"BILL-{today:%Y%m%d}-{sequence:08d}-{unique}"
    customer_id = f"CUST-{unique}"
    account_id = f"ACC-{uuid.uuid4().hex[:12].upper()}"
    item_one = f"ITEM-{uuid.uuid4().hex[:10].upper()}"
    item_two = f"ITEM-{uuid.uuid4().hex[:10].upper()}"

    # Vary the line values per request while keeping the invoice internally consistent.
    amount_one = 8000 + (sequence * 137) % 5000
    amount_two = 500 + (sequence * 83) % 3000
    total_amount = amount_one + amount_two
    tax_amount = round(total_amount * 0.18, 2)
    net_amount = round(total_amount + tax_amount, 2)

    payload = {
        "requestId": request_id,
        "sourceSystem": "BSS-BILLING",
        "customerId": customer_id,
        "accountId": account_id,
        "invoiceDate": today.isoformat(),
        "billingPeriod": {
            "from": today.replace(day=1).isoformat(),
            "to": today.isoformat(),
        },
        "currency": "INR",
        "totalAmount": float(total_amount),
        "taxAmount": tax_amount,
        "netAmount": net_amount,
        "lineItems": [
            {
                "itemId": item_one,
                "description": "Postpaid Mobile Services",
                "quantity": 1,
                "amount": float(amount_one),
            },
            {
                "itemId": item_two,
                "description": "Data Services",
                "quantity": 1,
                "amount": float(amount_two),
            },
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Correlation-Id": request_id,
    }
    return request_id, payload, headers


async def run(args) -> int:
    total = math.ceil(args.tps * args.duration)
    timeout = httpx.Timeout(args.timeout, connect=min(args.timeout, 5.0))
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    results = []
    pending: set[asyncio.Task] = set()
    skipped = 0
    run_started = time.perf_counter()

    print(f"Target: {args.tps:g} TPS for {args.duration:g}s ({total} invoices)")
    print(f"URL: {args.url}")
    print(f"Concurrency limit: {args.concurrency}; HTTP keep-alive enabled")
    print("Each request uses unique request, customer, account, and line-item IDs")
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        async def send_one(sequence: int, scheduled_at: float):
            request_id, payload, headers = make_invoice(sequence)
            started = time.perf_counter()
            try:
                response = await client.post(args.url, json=payload, headers=headers)
                server_elapsed = response.headers.get("X-Simulator-Elapsed-Ms")
                result = {
                    "status": response.status_code,
                    "request_id": request_id,
                    "error": None,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "start_offset": started - run_started,
                    "schedule_lag_ms": max(0.0, (started - scheduled_at) * 1000),
                    "server_elapsed_ms": float(server_elapsed) if server_elapsed else None,
                }
                return result
            except httpx.HTTPError as exc:
                result = {
                    "status": None,
                    "request_id": request_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "start_offset": started - run_started,
                    "schedule_lag_ms": max(0.0, (started - scheduled_at) * 1000),
                    "server_elapsed_ms": None,
                }
                return result

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
    errors = []
    for item in results:
        status = str(item["status"]) if item["status"] is not None else "network_error"
        outcomes[status] = outcomes.get(status, 0) + 1
        if item["status"] is None or not 200 <= item["status"] < 300:
            failures += 1
        if item["error"] and len(errors) < 5:
            errors.append(item["error"])

    starts = sorted(item["start_offset"] for item in results)
    actual_tps = (len(starts) - 1) / (starts[-1] - starts[0]) if len(starts) > 1 and starts[-1] > starts[0] else 0.0
    print("\nResults (immediate ACK path)")
    print(f"  completed: {len(results)} / {total}")
    print(f"  skipped at concurrency limit: {skipped}")
    print(f"  non-2xx/network failures: {failures}")
    print(f"  wall time including final ACKs: {elapsed:.2f}s")
    print(f"  observed request start rate: {actual_tps:.1f} TPS")
    print(f"  HTTP outcomes: {outcomes}")
    if latencies:
        print(f"  ACK latency ms: p50={percentile(latencies, 50):.1f}, p95={percentile(latencies, 95):.1f}, p99={percentile(latencies, 99):.1f}, mean={statistics.mean(latencies):.1f}")
    print(f"  schedule lag ms: p95={percentile(schedule_lags, 95):.1f}, max={max(schedule_lags, default=0):.1f}")
    if server_latencies:
        print(f"  server processing ms: p50={percentile(server_latencies, 50):.1f}, p95={percentile(server_latencies, 95):.1f}, p99={percentile(server_latencies, 99):.1f}")
    for error in errors:
        print(f"  error: {error}")
    print("  Note: this measures invoice ACKs; check callback receiver/job status separately for callback delivery.")
    return 0 if results and skipped == 0 and failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test the asynchronous ERP invoice simulator scenario")
    parser.add_argument("--url", default="http://localhost:8088/sim/erp-invoice")
    parser.add_argument("--tps", type=float, default=200)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--concurrency", type=int, default=500)
    args = parser.parse_args()
    if args.tps <= 0 or args.duration <= 0 or args.timeout <= 0 or args.concurrency <= 0:
        parser.error("--tps, --duration, --timeout, and --concurrency must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
