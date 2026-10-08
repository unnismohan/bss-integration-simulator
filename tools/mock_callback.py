#!/usr/bin/env python3
"""Concurrent local receiver for load testing asynchronous simulator callbacks."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class CallbackStats:
    def __init__(self):
        self._lock = threading.Lock()
        self._interval = {"received": 0, "succeeded": 0, "failed": 0}
        self._total = {"received": 0, "succeeded": 0, "failed": 0}
        self._in_flight = 0

    def received(self):
        with self._lock:
            self._interval["received"] += 1
            self._total["received"] += 1
            self._in_flight += 1

    def finished(self, success):
        with self._lock:
            key = "succeeded" if success else "failed"
            self._interval[key] += 1
            self._total[key] += 1
            self._in_flight -= 1

    def snapshot(self):
        with self._lock:
            interval = self._interval
            self._interval = {"received": 0, "succeeded": 0, "failed": 0}
            return interval, dict(self._total), self._in_flight


STATS = CallbackStats()


class CallbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        STATS.received()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative Content-Length")
            self.rfile.read(length)
            response = json.dumps({"received": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            STATS.finished(True)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            STATS.finished(False)

    def log_message(self, fmt, *args):
        # Access logs are intentionally suppressed; a periodic counter summary is more useful under load.
        return


class ConcurrentCallbackServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 1024

    def __init__(self, address, handler, max_workers):
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        self._worker_slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


def report_status(stop_event, interval):
    while not stop_event.wait(interval):
        recent, total, in_flight = STATS.snapshot()
        print(
            f"Last {interval:g}s: received={recent['received']}, success={recent['succeeded']}, "
            f"failed={recent['failed']} | totals: received={total['received']}, "
            f"success={total['succeeded']}, failed={total['failed']}, in_flight={in_flight}",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description="Receive and count test callbacks from the BSS simulator")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=3001, help="Listening port")
    parser.add_argument("--workers", type=int, default=256, help="Maximum concurrent callback handlers (default: 256)")
    parser.add_argument("--status-interval", type=float, default=5, help="Seconds between summary lines (default: 5)")
    args = parser.parse_args()
    if args.workers < 1 or args.status_interval <= 0:
        parser.error("--workers and --status-interval must be greater than zero")

    server = ConcurrentCallbackServer((args.host, args.port), CallbackHandler, args.workers)
    stop_event = threading.Event()
    status_thread = threading.Thread(target=report_status, args=(stop_event, args.status_interval), daemon=True)
    status_thread.start()
    print(
        f"Listening for callbacks at http://{args.host}:{args.port}/callback "
        f"(max concurrent handlers: {args.workers}; status every {args.status_interval:g}s)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping callback receiver", flush=True)
    finally:
        stop_event.set()
        status_thread.join(timeout=2)
        server.server_close()
        recent, total, in_flight = STATS.snapshot()
        print(
            f"Final totals: received={total['received']}, success={total['succeeded']}, "
            f"failed={total['failed']}, in_flight={in_flight}",
            flush=True,
        )


if __name__ == "__main__":
    main()
