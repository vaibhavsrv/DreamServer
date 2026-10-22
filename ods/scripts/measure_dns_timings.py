#!/usr/bin/env python3
"""
Measure per-hostname getaddrinfo duration and total /status time.
Runs on windows-latest and ubuntu-latest.
"""

import os
import sys
import time
import json
import socket
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

def main():
    print("===============================================================")
    print("      ODS Resolver DNS Timings & /status Measurement           ")
    print("===============================================================")
    print(f"Platform: {sys.platform} (os.name={os.name})")
    print(f"Python: {sys.version}")

    repo_root = Path(__file__).resolve().parents[2]
    api_dir = repo_root / "ods" / "extensions" / "services" / "dashboard-api"
    logs_dir = repo_root / "ci-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    os.environ["ODS_EXTENSIONS_DIR"] = str(repo_root / "ods" / "extensions" / "services")
    os.environ["ODS_INSTALL_DIR"] = str(repo_root / "ods")
    sys.path.insert(0, str(api_dir))

    from config import load_extension_manifests, EXTENSIONS_DIR, GPU_BACKEND

    services, _, _ = load_extension_manifests(EXTENSIONS_DIR, GPU_BACKEND)
    print(f"Loaded {len(services)} services from extension manifests.\n")

    # Step 1: Measure per-hostname getaddrinfo duration
    print("--- STEP 1: Per-Hostname getaddrinfo Timings ---")
    dns_results = []
    total_dns_duration = 0.0

    # Ensure unique hostnames with their primary port
    unique_hosts = {}
    for sid, cfg in sorted(services.items()):
        host = cfg.get("host", "localhost")
        port = int(cfg.get("port", 80) or 80)
        if host not in unique_hosts:
            unique_hosts[host] = (sid, port)

    print(f"{'Service ID':<25} {'Hostname':<28} {'Port':<6} {'Elapsed (ms)':<14} {'Result / Error':<30}")
    print("-" * 105)

    for host, (sid, port) in unique_hosts.items():
        t0 = time.perf_counter()
        error_msg = None
        addr_result = None
        try:
            addr_result = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        except Exception as e:
            error_msg = str(e)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        total_dns_duration += elapsed_ms / 1000

        res_str = f"Resolved: {addr_result[0][4][0]}" if addr_result else f"Failed: {error_msg}"
        print(f"{sid:<25} {host:<28} {port:<6} {elapsed_ms:>10.2f} ms   {res_str:<30}")

        dns_results.append({
            "service_id": sid,
            "hostname": host,
            "port": port,
            "elapsed_ms": round(elapsed_ms, 2),
            "success": addr_result is not None,
            "resolved_ip": addr_result[0][4][0] if addr_result else None,
            "error": error_msg
        })

    print("-" * 105)
    print(f"Total sequential DNS probe time: {total_dns_duration:.2f} seconds\n")

    # Step 2: Measure total /status time via Uvicorn
    print("--- STEP 2: Measure Total /status Duration on Uvicorn ---")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    env = os.environ.copy()
    env["DASHBOARD_API_KEY"] = "timing-test-key-999"
    env["ODS_INSTALL_DIR"] = str(repo_root / "ods")
    env["ODS_DATA_DIR"] = str(repo_root / "data" / "timing_stub")
    env["ODS_EXTENSIONS_DIR"] = str(repo_root / "ods" / "extensions" / "services")
    env["PYTHONPATH"] = str(api_dir)
    env["SERVICE_HOST"] = "127.0.0.1"

    (repo_root / "data" / "timing_stub").mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--app-dir", str(api_dir),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--log-level", "warning"
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    # Poll /health until ready
    health_url = f"http://127.0.0.1:{port}/health"
    headers = {"Authorization": "Bearer timing-test-key-999"}
    ready = False
    start_poll = time.time()
    while time.time() - start_poll < 20:
        if proc.poll() is not None:
            break
        try:
            req = urllib.request.Request(health_url, headers=headers)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.3)

    if not ready:
        print(f"ERROR: uvicorn failed to start on port {port}!")
        proc.kill()
        sys.exit(1)

    print(f"Uvicorn is ready on port {port}. Triggering GET /status...")
    status_url = f"http://127.0.0.1:{port}/status"
    status_t0 = time.perf_counter()
    status_code = None
    status_body_len = 0
    status_error = None

    try:
        req = urllib.request.Request(status_url, headers=headers)
        # Give generous 45s timeout to measure exact completion time
        with urllib.request.urlopen(req, timeout=45.0) as resp:
            status_code = resp.status
            body = resp.read()
            status_body_len = len(body)
    except Exception as e:
        status_error = str(e)

    status_elapsed = time.perf_counter() - status_t0
    print(f"GET /status result: code={status_code}, error={status_error}, body_bytes={status_body_len}")
    print(f"Measured GET /status duration: {status_elapsed:.3f} seconds ({status_elapsed*1000:.1f} ms)")

    # Clean shutdown
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    summary = {
        "platform": sys.platform,
        "os_name": os.name,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_sequential_dns_ms": round(total_dns_duration * 1000, 2),
        "status_endpoint_elapsed_ms": round(status_elapsed * 1000, 2),
        "status_endpoint_status_code": status_code,
        "status_endpoint_error": status_error,
        "per_hostname_dns": dns_results
    }

    out_file = logs_dir / f"dns_timings_{sys.platform}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSaved timing summary to {out_file}")

if __name__ == "__main__":
    main()
