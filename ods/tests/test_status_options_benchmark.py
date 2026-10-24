#!/usr/bin/env python3
"""
Measurement of /status service health options under the supervisor's real environment:
(a) As is
(b) Every manifest host_env set to 127.0.0.1
(c) Every manifest host_env set to 127.0.0.2

Simulates the supervisor state: ONLY llama-server (port 8080) and dashboard-api (port 3002) are running.
Evaluates total /status time (concurrent fan-out) and detects any FALSE GREEN (services reporting healthy when not running).
"""

import concurrent.futures
import http.client
import http.server
import json
import os
import platform
import re
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXT_DIR = REPO_ROOT / "ods" / "extensions" / "services"

# 1. Start mock llama-server on 127.0.0.1:8080
class MockLlamaHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

# 2. Start mock dashboard-api on 127.0.0.1:3002
class MockDashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/api/status", "/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def start_mock_server(port, handler_cls):
    server = socketserver.TCPServer(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server

def parse_yaml_fallback(text):
    data = {}
    svc = {}
    in_svc = False
    for line in text.splitlines():
        if line.startswith("service:"):
            in_svc = True
            continue
        if in_svc:
            if line and not line.startswith(" ") and not line.startswith("\t"):
                in_svc = False
                continue
            m = re.match(r"^\s*([a-zA-Z0-9_-]+)\s*:\s*(.*)$", line)
            if m:
                k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
                svc[k] = v
    data["service"] = svc
    return data

# Load service manifests
def load_manifests():
    services = []
    for p in sorted(EXT_DIR.glob("*/manifest.yaml")):
        try:
            content = p.read_text(encoding="utf-8")
            try:
                import yaml
                data = yaml.safe_load(content) or {}
            except Exception:
                data = parse_yaml_fallback(content)
            svc = data.get("service", {})
            services.append({
                "id": svc.get("id", p.parent.name),
                "name": svc.get("name", p.parent.name),
                "host_env": svc.get("host_env"),
                "default_host": svc.get("default_host", "localhost"),
                "port": int(svc.get("port", 0)),
                "health": svc.get("health", "/"),
            })
        except Exception as e:
            print(f"Error loading {p}: {e}")
    return services

def probe_single_service(svc, env_overrides, map_unconfigured_to_ip, timeout=2.0):
    sid = svc["id"]
    h_env = svc["host_env"]
    d_host = svc["default_host"]
    
    # Host resolution mimicking dashboard-api config.py
    if h_env and env_overrides.get(h_env):
        resolved_host = env_overrides[h_env]
    elif map_unconfigured_to_ip:
        resolved_host = map_unconfigured_to_ip
    else:
        resolved_host = d_host
    
    port = svc["port"]
    health = svc["health"]
    
    t0 = time.perf_counter()
    if port == 0:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "id": sid, "host": resolved_host, "port": port, "health": health,
            "status": "not_deployed", "elapsed_ms": elapsed_ms, "details": "Port 0",
            "is_false_green": False
        }
    
    try:
        conn = http.client.HTTPConnection(resolved_host, port, timeout=timeout)
        conn.request("GET", health)
        resp = conn.getresponse()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # Under dashboard-api rules: 200..399 is considered healthy
        status = "healthy" if resp.status < 400 else "down"
        details = f"HTTP {resp.status}"
        conn.close()
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        status = "down"
        details = f"{type(exc).__name__}: {str(exc)[:30]}"
    
    # Ground truth: Only llama-server and dashboard-api are actually running
    actually_running = sid in ("llama-server", "dashboard-api")
    is_false_green = (status == "healthy" and not actually_running)
    
    return {
        "id": sid, "host": resolved_host, "port": port, "health": health,
        "status": status, "elapsed_ms": elapsed_ms, "details": details,
        "is_false_green": is_false_green
    }

def run_benchmark_for_condition(condition_name, services, env_overrides, map_unconfigured_to_ip=None):
    print("\n" + "=" * 80)
    print(f"CONDITION: {condition_name} ({platform.system()} {platform.release()})")
    print("=" * 80)
    
    t0 = time.perf_counter()
    results = []
    
    # Run all probes concurrently (mimicking asyncio.gather in dashboard-api get_all_services)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(services), 1)) as executor:
        futures = {
            executor.submit(probe_single_service, svc, env_overrides, map_unconfigured_to_ip): svc
            for svc in services
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    
    total_elapsed_ms = (time.perf_counter() - t0) * 1000.0
    
    # Sort results alphabetically by service id
    results.sort(key=lambda r: r["id"])
    false_greens = [r["id"] for r in results if r["is_false_green"]]
    
    print(f"Total /status Concurrent Probing Duration: {total_elapsed_ms:.2f} ms ({total_elapsed_ms/1000.0:.2f} s)")
    print(f"False Greens Detected: {len(false_greens)} ({', '.join(false_greens) if false_greens else 'NONE'})\n")
    
    print(f"{'Service ID':<22} | {'Resolved Host':<14} | {'Port':<6} | {'Status':<10} | {'Time (ms)':<10} | {'False Green?':<12} | Details")
    print("-" * 95)
    for r in results:
        fg_str = "YES (BUG!)" if r["is_false_green"] else "No"
        print(f"{r['id']:<22} | {r['host']:<14} | {r['port']:<6} | {r['status']:<10} | {r['elapsed_ms']:<10.2f} | {fg_str:<12} | {r['details']}")
    print("-" * 95)
    
    # Specific inspection of requested services
    print("\nTargeted Service Inspection:")
    for sid in ("open-webui", "searxng", "token-spy", "llama-server", "dashboard-api"):
        matching = [r for r in results if r["id"] == sid]
        if matching:
            r = matching[0]
            fg_label = " [*** FALSE GREEN ***]" if r["is_false_green"] else ""
            print(f"  * {sid:<15}: status={r['status']} (host={r['host']}:{r['port']}{r['health']}, time={r['elapsed_ms']:.2f} ms){fg_label}")
    
    return total_elapsed_ms, results, false_greens

def main():
    # Start mock servers
    try:
        llama_srv = start_mock_server(8080, MockLlamaHandler)
        print("Mock llama-server listening on 127.0.0.1:8080 (serves /health -> 200)")
    except Exception as e:
        print(f"Warning: Could not bind 8080 ({e})")
        llama_srv = None

    try:
        dash_srv = start_mock_server(3002, MockDashboardHandler)
        print("Mock dashboard-api listening on 127.0.0.1:3002 (serves /health -> 200)")
    except Exception as e:
        print(f"Warning: Could not bind 3002 ({e})")
        dash_srv = None

    services = load_manifests()
    print(f"Loaded {len(services)} extension manifests.\n")
    
    all_host_envs = [s["host_env"] for s in services if s.get("host_env")]
    
    # (a) As is
    env_a = {h: None for h in all_host_envs}
    env_a["SERVICE_HOST"] = "127.0.0.1"
    time_a, res_a, fg_a = run_benchmark_for_condition(
        "Option (a): As Is (SERVICE_HOST=127.0.0.1, manifests default)",
        services, env_a
    )
    
    # (b) Every manifest host_env set to 127.0.0.1
    env_b = {h: "127.0.0.1" for h in all_host_envs}
    env_b["SERVICE_HOST"] = "127.0.0.1"
    time_b, res_b, fg_b = run_benchmark_for_condition(
        "Option (b): Every Manifest host_env Set to 127.0.0.1 (all services to 127.0.0.1)",
        services, env_b, map_unconfigured_to_ip="127.0.0.1"
    )
    
    # (c) Every manifest host_env set to 127.0.0.2
    env_c = {h: "127.0.0.2" for h in all_host_envs}
    env_c["SERVICE_HOST"] = "127.0.0.2"
    time_c, res_c, fg_c = run_benchmark_for_condition(
        "Option (c): Every Manifest host_env Set to 127.0.0.2",
        services, env_c, map_unconfigured_to_ip="127.0.0.2"
    )
    
    print("\n" + "=" * 80)
    print("EXECUTIVE SUMMARY OF /STATUS OPTIONS")
    print("=" * 80)
    print(f"{'Option':<45} | {'Total Duration':<15} | {'False Greens':<15}")
    print("-" * 80)
    print(f"{'Option (a) As is':<45} | {time_a:<15.2f} ms | {len(fg_a)} ({', '.join(fg_a) if fg_a else 'none'})")
    print(f"{'Option (b) host_env = 127.0.0.1':<45} | {time_b:<15.2f} ms | {len(fg_b)} ({', '.join(fg_b) if fg_b else 'none'})")
    print(f"{'Option (c) host_env = 127.0.0.2':<45} | {time_c:<15.2f} ms | {len(fg_c)} ({', '.join(fg_c) if fg_c else 'none'})")
    print("=" * 80)

if __name__ == "__main__":
    main()
