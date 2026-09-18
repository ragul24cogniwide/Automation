#!/usr/bin/env python3
"""
Render & Neon Keep-Alive Daemon
Pings the backend service every 10 seconds to prevent Render free-tier spin-down
and eliminate 50+ second cold-boot latency.
"""

import sys
import time
import argparse
import urllib.request
import urllib.error
import json
from datetime import datetime

DEFAULT_URL = "https://agent-automation-e5dh.onrender.com"
DEFAULT_INTERVAL = 10  # 10 seconds


def ping_endpoint(base_url: str, endpoint: str = "/api/health") -> tuple[bool, int, float, str]:
    """
    Sends a GET request to the target endpoint.
    Returns: (is_success, status_code, latency_ms, details)
    """
    full_url = base_url.rstrip("/") + endpoint
    start_time = time.time()
    try:
        req = urllib.request.Request(
            full_url,
            headers={
                "User-Agent": "AuraAI-KeepAlive-Daemon/1.0",
                "Accept": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            latency_ms = (time.time() - start_time) * 1000
            status_code = response.getcode()
            body = response.read().decode("utf-8")
            try:
                parsed = json.loads(body)
                db_status = parsed.get("database", "ok")
                details = f"DB: {db_status}"
            except Exception:
                details = f"Body: {body[:60]}"
            return True, status_code, latency_ms, details
    except urllib.error.HTTPError as e:
        latency_ms = (time.time() - start_time) * 1000
        return False, e.code, latency_ms, f"HTTP Error: {e.reason}"
    except urllib.error.URLError as e:
        latency_ms = (time.time() - start_time) * 1000
        return False, 0, latency_ms, f"URL Error: {e.reason}"
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        return False, 0, latency_ms, f"Error: {str(e)}"


def run_keep_alive(url: str, interval: int = 10, endpoint: str = "/api/health", once: bool = False):
    print("=" * 60)
    print("AURA AI - RENDER 24/7 KEEP-ALIVE DAEMON")
    print(f"Target URL : {url.rstrip('/')}{endpoint}")
    print(f"Interval   : {interval} seconds")
    print(f"Mode       : {'Single Check' if once else 'Continuous 24/7'}")
    print("=" * 60)

    consecutive_success = 0
    consecutive_fails = 0

    while True:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success, code, latency, details = ping_endpoint(url, endpoint)

        if success:
            consecutive_success += 1
            consecutive_fails = 0
            print(f"[{timestamp}] [OK {code}] Latency: {latency:.1f}ms | {details} (Streak: {consecutive_success})")
        else:
            consecutive_fails += 1
            consecutive_success = 0
            print(f"[{timestamp}] [FAIL {code}] Latency: {latency:.1f}ms | {details} (Fails: {consecutive_fails})")

        if once:
            sys.exit(0 if success else 1)

        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render 24/7 Keep-Alive Daemon")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Base backend URL (default: {DEFAULT_URL})")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Ping interval in seconds (default: 10)")
    parser.add_argument("--endpoint", default="/api/health", help="Endpoint to ping (/api/health or /api/ping)")
    parser.add_argument("--once", action="store_true", help="Run once and exit (for health checks / verification)")

    args = parser.parse_args()
    run_keep_alive(url=args.url, interval=args.interval, endpoint=args.endpoint, once=args.once)
