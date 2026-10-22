#!/usr/bin/env python3
"""
Windows-Native Proof of Concept Launcher
Runs on windows-latest without Docker or WSL.
Downloads pinned llama-server.exe release and a tiny GGUF model,
supervises llama-server.exe and dashboard-api as native processes,
waits for health checks, performs a chat completion, and shuts down cleanly.
"""

import os
import sys
import time
import json
import zipfile
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

LLAMA_RELEASE_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b9014/llama-b9014-bin-win-cpu-x64.zip"
LLAMA_ZIP_NAME = "llama-b9014-bin-win-cpu-x64.zip"

MODEL_URL = "https://huggingface.co/unsloth/SmolLM2-135M-Instruct-GGUF/resolve/main/SmolLM2-135M-Instruct-Q4_K_M.gguf"
MODEL_FILENAME = "SmolLM2-135M-Instruct-Q4_K_M.gguf"

LLAMA_PORT = 8080
DASHBOARD_PORT = 3002

def download_file(url: str, dest_path: Path) -> dict:
    print(f"Downloading {url} -> {dest_path}...")
    start_time = time.perf_counter()
    req = urllib.request.Request(url, headers={"User-Agent": "ODS-Windows-PoC/1.0"})
    
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as f:
        total_size = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 1024 * 1024
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            
    elapsed = time.perf_counter() - start_time
    file_size = dest_path.stat().st_size
    speed_mb = (file_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    print(f"Downloaded {file_size:,} bytes in {elapsed:.2f}s ({speed_mb:.1f} MB/s).")
    return {
        "url": url,
        "destination": str(dest_path),
        "bytes": file_size,
        "elapsed_seconds": round(elapsed, 3),
        "speed_mbs": round(speed_mb, 2)
    }

def poll_health(url: str, timeout: float = 30.0, headers: dict = None) -> tuple[bool, float]:
    start = time.perf_counter()
    headers = headers or {}
    while time.perf_counter() - start < timeout:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    elapsed = time.perf_counter() - start
                    return True, elapsed
        except Exception:
            time.sleep(0.5)
    return False, time.perf_counter() - start

def main():
    print("===============================================================")
    print("      ODS Windows-Native Proof of Concept Launcher             ")
    print("===============================================================")
    print(f"Platform: {sys.platform} (os.name={os.name})")
    print(f"Python: {sys.version}")
    print(f"Working Directory: {os.getcwd()}")
    start_wall = time.time()
    
    repo_root = Path(__file__).resolve().parents[2]
    poc_dir = repo_root / "data" / "poc_windows_native"
    runtime_dir = poc_dir / "runtime"
    models_dir = poc_dir / "models"
    logs_dir = repo_root / "ci-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": sys.platform,
        "python_version": sys.version,
        "steps": {}
    }

    # Step 1: Download pinned llama-server release
    print("\n--- STEP 1: Download Pinned llama-server.exe Release ---")
    zip_dest = poc_dir / LLAMA_ZIP_NAME
    summary["steps"]["download_runtime"] = download_file(LLAMA_RELEASE_URL, zip_dest)
    
    # Extract runtime
    print(f"Extracting {zip_dest} -> {runtime_dir}...")
    extract_start = time.perf_counter()
    with zipfile.ZipFile(zip_dest, "r") as zf:
        zf.extractall(runtime_dir)
    summary["steps"]["extract_runtime"] = {
        "elapsed_seconds": round(time.perf_counter() - extract_start, 3),
        "files_extracted": len(list(runtime_dir.glob("*")))
    }
    
    llama_exe = runtime_dir / "llama-server.exe"
    if not llama_exe.exists():
        # Might be in a subfolder
        matches = list(runtime_dir.glob("**/llama-server.exe"))
        if matches:
            llama_exe = matches[0]
        else:
            print(f"ERROR: llama-server.exe not found in {runtime_dir}!", file=sys.stderr)
            sys.exit(1)
            
    print(f"Found llama-server.exe at: {llama_exe}")
    
    # Step 1b: Verify --version
    ver_cmd = [str(llama_exe), "--version"]
    ver_res = subprocess.run(ver_cmd, capture_output=True, text=True)
    ver_out = (ver_res.stdout + ver_res.stderr).strip()
    print(f"llama-server version: {ver_out}")
    summary["steps"]["llama_server_version"] = ver_out

    # Step 2: Download Pinned Tiny GGUF Model
    print("\n--- STEP 2: Download Pinned Tiny GGUF Model ---")
    model_dest = models_dir / MODEL_FILENAME
    summary["steps"]["download_model"] = download_file(MODEL_URL, model_dest)

    # Step 3: Supervised Native Process Startup
    print("\n--- STEP 3: Start Native Supervised Processes ---")
    
    # 3a. Start llama-server.exe
    llama_cmd = [
        str(llama_exe),
        "--model", str(model_dest),
        "--alias", MODEL_FILENAME,
        "--host", "127.0.0.1",
        "--port", str(LLAMA_PORT),
        "--ctx-size", "2048",
        "--parallel", "1",
        "--threads", "4",
        "--no-webui"
    ]
    print(f"Spawning llama-server.exe on 127.0.0.1:{LLAMA_PORT}...")
    llama_log_path = logs_dir / "llama_server_stdout.log"
    llama_log = open(llama_log_path, "w", encoding="utf-8")
    llama_start_time = time.perf_counter()
    llama_proc = subprocess.Popen(llama_cmd, stdout=llama_log, stderr=subprocess.STDOUT)
    print(f"llama-server.exe PID: {llama_proc.pid}")

    # 3b. Start dashboard-api uvicorn
    api_dir = repo_root / "ods" / "extensions" / "services" / "dashboard-api"
    api_cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--app-dir", str(api_dir),
        "--host", "127.0.0.1",
        "--port", str(DASHBOARD_PORT),
        "--log-level", "info"
    ]
    api_env = os.environ.copy()
    api_env["DASHBOARD_API_KEY"] = "poc-native-key-999"
    api_env["ODS_INSTALL_DIR"] = str(repo_root / "ods")
    api_env["ODS_DATA_DIR"] = str(poc_dir / "ods_data")
    api_env["ODS_EXTENSIONS_DIR"] = str(repo_root / "ods" / "extensions" / "services")
    api_env["PYTHONPATH"] = str(api_dir)
    api_env["SERVICE_HOST"] = "127.0.0.1"
    api_env["OLLAMA_HOST"] = "127.0.0.1"
    api_env["OLLAMA_PORT"] = str(LLAMA_PORT)
    api_env["LLAMA_SERVER_PORT"] = str(LLAMA_PORT)
    
    (poc_dir / "ods_data").mkdir(parents=True, exist_ok=True)
    
    print(f"Spawning dashboard-api on 127.0.0.1:{DASHBOARD_PORT}...")
    api_log_path = logs_dir / "dashboard_api_stdout.log"
    api_log = open(api_log_path, "w", encoding="utf-8")
    api_start_time = time.perf_counter()
    api_proc = subprocess.Popen(api_cmd, env=api_env, stdout=api_log, stderr=subprocess.STDOUT)
    print(f"dashboard-api PID: {api_proc.pid}")

    # Step 4: Wait for Health
    print("\n--- STEP 4: Poll Health Probes ---")
    llama_healthy, llama_health_time = poll_health(f"http://127.0.0.1:{LLAMA_PORT}/health", timeout=30.0)
    print(f"llama-server /health: {'HEALTHY' if llama_healthy else 'FAILED'} (took {llama_health_time:.2f}s)")

    dashboard_healthy, dashboard_health_time = poll_health(
        f"http://127.0.0.1:{DASHBOARD_PORT}/health",
        timeout=30.0,
        headers={"Authorization": "Bearer poc-native-key-999"}
    )
    print(f"dashboard-api /health: {'HEALTHY' if dashboard_healthy else 'FAILED'} (took {dashboard_health_time:.2f}s)")

    summary["steps"]["process_supervision"] = {
        "llama_pid": llama_proc.pid,
        "llama_healthy": llama_healthy,
        "llama_ready_seconds": round(llama_health_time, 2),
        "dashboard_pid": api_proc.pid,
        "dashboard_healthy": dashboard_healthy,
        "dashboard_ready_seconds": round(dashboard_health_time, 2)
    }

    if not llama_healthy or not dashboard_healthy:
        print("ERROR: Health checks failed! Terminating processes.", file=sys.stderr)
        cleanup_processes(llama_proc, api_proc, llama_log, api_log)
        sys.exit(1)

    # Step 5: Send Chat Completion Through API
    print("\n--- STEP 5: Send Chat Completion Through API ---")
    prompt_payload = {
        "model": MODEL_FILENAME,
        "messages": [
            {"role": "system", "content": "You are a concise AI assistant."},
            {"role": "user", "content": "Respond with exactly: NATIVE_WINDOWS_AI_VERIFIED"}
        ],
        "temperature": 0.0,
        "max_tokens": 16,
        "stream": False
    }
    
    chat_url = f"http://127.0.0.1:{LLAMA_PORT}/v1/chat/completions"
    chat_start = time.perf_counter()
    req = urllib.request.Request(
        chat_url,
        data=json.dumps(prompt_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    
    with urllib.request.urlopen(req, timeout=30) as resp:
        chat_code = resp.status
        chat_body = json.loads(resp.read().decode("utf-8"))
        
    chat_elapsed = time.perf_counter() - chat_start
    message_content = chat_body.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    usage = chat_body.get("usage", {})
    
    print(f"Chat completion returned HTTP {chat_code} in {chat_elapsed:.2f}s.")
    print(f"Response Content: {message_content}")
    print(f"Token Usage: {usage}")
    
    # Probe Dashboard Endpoints
    dash_endpoints = ["/health", "/api/preflight/required-ports", "/disk"]
    dash_results = {}
    for ep in dash_endpoints:
        ep_req = urllib.request.Request(
            f"http://127.0.0.1:{DASHBOARD_PORT}{ep}",
            headers={"Authorization": "Bearer poc-native-key-999"}
        )
        try:
            with urllib.request.urlopen(ep_req, timeout=5) as ep_resp:
                dash_results[ep] = {"status": ep_resp.status, "body": ep_resp.read().decode("utf-8", "replace")[:100]}
        except Exception as e:
            dash_results[ep] = {"error": str(e)}

    summary["steps"]["chat_completion"] = {
        "http_status": chat_code,
        "elapsed_seconds": round(chat_elapsed, 3),
        "content": message_content,
        "usage": usage,
        "dashboard_endpoints": dash_results
    }

    # Step 6: Supervised Clean Shutdown
    print("\n--- STEP 6: Supervised Clean Shutdown ---")
    shutdown_start = time.perf_counter()
    cleanup_processes(llama_proc, api_proc, llama_log, api_log)
    shutdown_elapsed = time.perf_counter() - shutdown_start
    print(f"Clean shutdown completed in {shutdown_elapsed:.2f}s.")
    summary["steps"]["clean_shutdown"] = {
        "elapsed_seconds": round(shutdown_elapsed, 3),
        "status": "SUCCESS"
    }
    
    total_time = time.time() - start_wall
    summary["total_runtime_seconds"] = round(total_time, 2)
    print(f"\nSUCCESS: Native Windows Proof of Concept completed in {total_time:.2f}s.")
    
    # Save summary json
    summary_path = logs_dir / "poc_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"Saved PoC summary to {summary_path}")

def cleanup_processes(llama_proc, api_proc, llama_log, api_log):
    for name, proc, log in [("llama-server", llama_proc, llama_log), ("dashboard-api", api_proc, api_log)]:
        if proc and proc.poll() is None:
            print(f"Terminating {name} (PID {proc.pid})...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
                print(f"{name} exited cleanly with code {proc.returncode}.")
            except Exception:
                print(f"Force killing {name}...")
                proc.kill()
                proc.wait()
        if log:
            log.close()

if __name__ == "__main__":
    main()
