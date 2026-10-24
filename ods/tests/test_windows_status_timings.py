#!/usr/bin/env python3
"""
Measurement of hostname resolution and /status service health check timings
under the supervisor environment with SERVICE_HOST=127.0.0.1 vs defaults.
Runs on Windows and Linux to produce exact empirical comparison data.
"""

import asyncio
import os
import platform
import socket
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "ods" / "extensions" / "services" / "dashboard-api"
sys.path.insert(0, str(API_DIR))

# Pinned set of hostnames probed across ODS extension manifests
HOSTNAMES_TO_TEST = [
    "127.0.0.1",
    "localhost",
    "whisper",
    "llama-server",
    "litellm",
    "qdrant",
    "dashboard-api",
    "token-spy",
    "tts",
    "searxng",
    "perplexica",
    "open-webui",
]

def measure_hostname_resolutions():
    print("=" * 70)
    print(f"HOSTNAME RESOLUTION BENCHMARK ({platform.system()} {platform.release()})")
    print("=" * 70)
    print(f"{'Hostname':<22} | {'Result':<12} | {'Time (ms)':<10} | Details")
    print("-" * 70)
    
    results = {}
    for host in HOSTNAMES_TO_TEST:
        t0 = time.perf_counter()
        try:
            addrinfo = socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            ip = addrinfo[0][4][0] if addrinfo else "unknown"
            status = "RESOLVED"
            details = f"IP: {ip}"
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            status = "FAILED"
            details = f"{type(exc).__name__}: {str(exc)}"
        
        results[host] = {"status": status, "elapsed_ms": elapsed_ms, "details": details}
        print(f"{host:<22} | {status:<12} | {elapsed_ms:<10.2f} | {details}")
    
    print("-" * 70)
    return results

async def measure_service_health_checks():
    print("\n" + "=" * 70)
    print(f"SERVICE HEALTH CHECKS BENCHMARK ({platform.system()})")
    print("=" * 70)
    
    # Configure environment exactly as supervisor sets it
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        os.environ["SERVICE_HOST"] = "127.0.0.1"
        os.environ["ODS_INSTALL_DIR"] = str(REPO_ROOT / "ods")
        os.environ["ODS_DATA_DIR"] = str(tmp_path / "ods_data")
        os.environ["ODS_EXTENSIONS_DIR"] = str(REPO_ROOT / "ods" / "extensions" / "services")
        (tmp_path / "ods_data").mkdir(parents=True, exist_ok=True)
        
        try:
            import helpers
            print(f"Loaded helpers from {helpers.__file__}")
            print(f"Configured SERVICES count: {len(helpers.SERVICES)}")
            
            # Print sample host mappings
            print("\nSample service host mapping:")
            for sid in ["llama-server", "whisper", "litellm", "qdrant", "dashboard-api", "token-spy"]:
                if sid in helpers.SERVICES:
                    cfg = helpers.SERVICES[sid]
                    print(f"  {sid:<15} -> host={cfg.get('host')} port={cfg.get('port')} health={cfg.get('health')}")
            
            # 1. Measure live get_all_services()
            print("\nExecuting live get_all_services()...")
            t0 = time.perf_counter()
            statuses = await helpers.get_all_services()
            elapsed_all = (time.perf_counter() - t0) * 1000.0
            print(f"get_all_services() completed in {elapsed_all:.2f} ms ({len(statuses)} services probed)")
            
            # Print per-service status
            print(f"{'Service ID':<20} | {'Status':<10} | {'Resp Time (ms)':<15}")
            print("-" * 55)
            for s in statuses:
                resp_time = f"{s.response_time_ms:.2f}" if s.response_time_ms is not None else "None"
                print(f"{s.id:<20} | {s.status:<10} | {resp_time:<15}")
            
            return elapsed_all
        except Exception as exc:
            print(f"helpers module check unavailable ({exc}); falling back to standard library socket/HTTP probes")
            return measure_stdlib_health_probes()

def measure_stdlib_health_probes():
    import http.client
    print("\nExecuting standard library HTTP service health probes (timeout=2.0s)...")
    service_probes = [
        ("llama-server", "127.0.0.1", 8080, "/health"),
        ("dashboard-api", "127.0.0.1", 3002, "/health"),
        ("whisper", "whisper", 8000, "/health"),
        ("litellm", "litellm", 4000, "/health/readiness"),
        ("qdrant", "qdrant", 6333, "/"),
        ("token-spy", "token-spy", 8080, "/health"),
    ]
    print(f"{'Service ID':<15} | {'Host':<15} | {'Port':<6} | {'Status':<10} | {'Elapsed (ms)':<12} | Details")
    print("-" * 75)
    for sid, host, port, path in service_probes:
        t0 = time.perf_counter()
        try:
            conn = http.client.HTTPConnection(host, port, timeout=2.0)
            conn.request("GET", path)
            resp = conn.getresponse()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            status = "UP" if resp.status < 500 else "ERROR"
            details = f"HTTP {resp.status}"
            conn.close()
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            status = "DOWN"
            details = f"{type(exc).__name__}: {str(exc)[:35]}"
        print(f"{sid:<15} | {host:<15} | {port:<6} | {status:<10} | {elapsed_ms:<12.2f} | {details}")
    print("-" * 75)

def main():
    measure_hostname_resolutions()
    try:
        asyncio.run(measure_service_health_checks())
    except Exception as exc:
        print(f"Async measurement failed: {exc}")

if __name__ == "__main__":
    main()
