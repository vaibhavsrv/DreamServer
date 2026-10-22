#!/usr/bin/env python3
"""
CI probe for ODS Windows-Native feasibility experiments.
Modes:
  --import-host-agent : Imports ods/bin/ods-host-agent.py with stub environment.
  --probe-dashboard-api : Starts dashboard-api via uvicorn without Docker, tests /health + 3 endpoints.
"""

import sys
import os
import time
import socket
import json
import urllib.request
import urllib.error
import subprocess
import traceback
from pathlib import Path

def run_import_host_agent():
    print("=== STEP (c): Import ods/bin/ods-host-agent.py with stub environment ===")
    repo_root = Path(__file__).resolve().parents[2]
    bin_dir = repo_root / "ods" / "bin"
    agent_script = bin_dir / "ods-host-agent.py"
    
    stub_home = repo_root / "data" / "stub_ods"
    stub_home.mkdir(parents=True, exist_ok=True)
    
    os.environ["ODS_HOME"] = str(stub_home)
    os.environ["ODS_HOST_AGENT_TOKEN"] = "test-token-ci"
    sys.path.insert(0, str(bin_dir))
    
    print(f"Target script: {agent_script}")
    print(f"ODS_HOME: {os.environ['ODS_HOME']}")
    
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ods_host_agent", str(agent_script))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec for {agent_script}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print("SUCCESS: ods-host-agent.py imported cleanly without errors.")
        return 0
    except Exception as e:
        print(f"FAILURE: ods-host-agent.py import failed with {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

def run_probe_dashboard_api():
    print("=== STEP (d): Start uvicorn for dashboard-api without Docker ===")
    repo_root = Path(__file__).resolve().parents[2]
    api_dir = repo_root / "ods" / "extensions" / "services" / "dashboard-api"
    stub_data = repo_root / "data" / "stub_ods_data"
    stub_data.mkdir(parents=True, exist_ok=True)
    
    # Bind free port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    
    env = os.environ.copy()
    env["DASHBOARD_API_KEY"] = "ci-test-key-999"
    env["ODS_INSTALL_DIR"] = str(repo_root / "ods")
    env["ODS_DATA_DIR"] = str(stub_data)
    env["ODS_EXTENSIONS_DIR"] = str(repo_root / "ods" / "extensions" / "services")
    env["PYTHONPATH"] = str(api_dir)
    env["SERVICE_HOST"] = "127.0.0.1"
    
    print(f"Starting uvicorn on 127.0.0.1:{port} from {api_dir}...")
    cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--app-dir", str(api_dir),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--log-level", "info"
    ]
    
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    
    # Poll for readiness
    headers = {"Authorization": "Bearer ci-test-key-999"}
    health_url = f"http://127.0.0.1:{port}/health"
    ready = False
    start_time = time.time()
    
    print(f"Polling {health_url} for up to 25s...")
    while time.time() - start_time < 25:
        if proc.poll() is not None:
            print(f"ERROR: uvicorn process terminated early with exit code {proc.returncode}!")
            out, _ = proc.communicate()
            print("--- UVICORN OUTPUT ---")
            print(out)
            return 1
        try:
            req = urllib.request.Request(health_url, headers=headers)
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.5)
            
    if not ready:
        print("ERROR: uvicorn did not become ready within 25s!", file=sys.stderr)
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
            print("--- UVICORN OUTPUT ---")
            print(out)
        except Exception:
            proc.kill()
        return 1
        
    print(f"Uvicorn is ready on port {port} (startup took {time.time() - start_time:.2f}s). Testing endpoints...")
    
    endpoints = [
        "/health",
        "/status",
        "/services",
        "/api/preflight/required-ports"
    ]
    
    results = {}
    has_failure = False
    
    for ep in endpoints:
        url = f"http://127.0.0.1:{port}{ep}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                results[ep] = {"status_code": resp.status, "body_snippet": body[:120]}
                print(f"  GET {ep} -> HTTP {resp.status}: {body[:80]}")
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8", errors="replace")
            results[ep] = {"status_code": he.code, "error": body[:120]}
            print(f"  GET {ep} -> HTTP {he.code}: {body[:80]}", file=sys.stderr)
            has_failure = True
        except Exception as e:
            results[ep] = {"error": str(e)}
            print(f"  GET {ep} -> FAILED: {e}", file=sys.stderr)
            has_failure = True
            
    # Terminate process cleanly
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        
    print("\n--- Summary of endpoint tests ---")
    print(json.dumps(results, indent=2))
    
    if has_failure:
        print("At least one endpoint returned an error.", file=sys.stderr)
        return 1
        
    print("SUCCESS: All 4 endpoints answered cleanly without Docker.")
    return 0

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--import-host-agent":
        sys.exit(run_import_host_agent())
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-dashboard-api":
        sys.exit(run_probe_dashboard_api())
    else:
        print("Usage: ci-probe-dashboard-api.py [--import-host-agent | --probe-dashboard-api]")
        sys.exit(1)
