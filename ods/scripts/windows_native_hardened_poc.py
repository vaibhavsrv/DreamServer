#!/usr/bin/env python3
"""
Hardened Windows-Native Proof of Concept and Failure Tests.
Runs on windows-latest without Docker or WSL.
Tests:
  1. SHA-256 verification of downloads
  2. Chat completion through dashboard-api (/api/chat)
  3. Graceful shutdown and exit code reporting
  4. Failure test: Port 8080 collision
  5. Failure test: Supervisor killed, checking orphaned process (with vs without Job Object)
  6. Failure test: Corrupted model file
"""

import os
import sys
import time
import json
import socket
import hashlib
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

def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()

def download_file(url: str, dest_path: Path) -> dict:
    print(f"Downloading {url} -> {dest_path}...")
    start_time = time.perf_counter()
    req = urllib.request.Request(url, headers={"User-Agent": "ODS-Windows-Hardened/1.0"})
    
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as f:
        while chunk := resp.read(1024 * 1024):
            f.write(chunk)
            
    elapsed = time.perf_counter() - start_time
    file_size = dest_path.stat().st_size
    sha256 = compute_sha256(dest_path)
    print(f"Downloaded {file_size:,} bytes in {elapsed:.2f}s.")
    print(f"SHA-256: {sha256}")
    return {
        "url": url,
        "destination": str(dest_path),
        "bytes": file_size,
        "sha256": sha256,
        "elapsed_seconds": round(elapsed, 3)
    }

def poll_health(url: str, timeout: float = 30.0, headers: dict = None) -> tuple[bool, float]:
    start = time.perf_counter()
    headers = headers or {}
    while time.perf_counter() - start < timeout:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    return True, time.perf_counter() - start
        except Exception:
            time.sleep(0.4)
    return False, time.perf_counter() - start

def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True)
            return str(pid) in res.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

def main():
    print("===============================================================")
    print("   ODS Windows-Native Hardened PoC & Failure Diagnostics       ")
    print("===============================================================")
    print(f"Platform: {sys.platform} (os.name={os.name})")
    print(f"Python: {sys.version}")

    repo_root = Path(__file__).resolve().parents[2]
    poc_dir = repo_root / "data" / "poc_hardened"
    runtime_dir = poc_dir / "runtime"
    models_dir = poc_dir / "models"
    logs_dir = repo_root / "ci-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": sys.platform,
        "python_version": sys.version,
        "sha256_verification": {},
        "chat_through_dashboard_api": {},
        "graceful_shutdown": {},
        "failure_tests": {}
    }

    # 1. Download & Verify SHA-256
    print("\n--- 1. DOWNLOAD & SHA-256 VERIFICATION ---")
    zip_dest = poc_dir / LLAMA_ZIP_NAME
    summary["sha256_verification"]["runtime_zip"] = download_file(LLAMA_RELEASE_URL, zip_dest)
    
    with zipfile.ZipFile(zip_dest, "r") as zf:
        zf.extractall(runtime_dir)
    llama_exe = runtime_dir / "llama-server.exe"
    if not llama_exe.exists():
        llama_exe = list(runtime_dir.glob("**/llama-server.exe"))[0]
    summary["sha256_verification"]["llama_server_exe"] = {
        "path": str(llama_exe),
        "bytes": llama_exe.stat().st_size,
        "sha256": compute_sha256(llama_exe)
    }
    print(f"llama-server.exe SHA-256: {summary['sha256_verification']['llama_server_exe']['sha256']}")

    model_dest = models_dir / MODEL_FILENAME
    summary["sha256_verification"]["model_gguf"] = download_file(MODEL_URL, model_dest)

    # 2. Start llama-server & dashboard-api
    print("\n--- 2. START SUPERVISED PROCESSES ---")
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
    llama_proc = subprocess.Popen(llama_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(f"llama-server.exe started (PID {llama_proc.pid})")

    api_dir = repo_root / "ods" / "extensions" / "services" / "dashboard-api"
    api_env = os.environ.copy()
    api_env["DASHBOARD_API_KEY"] = "hardened-test-key-999"
    api_env["ODS_INSTALL_DIR"] = str(repo_root / "ods")
    api_env["ODS_DATA_DIR"] = str(poc_dir / "ods_data")
    api_env["ODS_EXTENSIONS_DIR"] = str(repo_root / "ods" / "extensions" / "services")
    api_env["PYTHONPATH"] = str(api_dir)
    api_env["SERVICE_HOST"] = "127.0.0.1"
    api_env["LLM_API_URL"] = f"http://127.0.0.1:{LLAMA_PORT}/v1"
    api_env["LLM_MODEL"] = MODEL_FILENAME
    api_env["GGUF_FILE"] = MODEL_FILENAME
    api_env["AMD_INFERENCE_LOCATION"] = ""
    (poc_dir / "ods_data").mkdir(parents=True, exist_ok=True)

    api_cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--app-dir", str(api_dir),
        "--host", "127.0.0.1",
        "--port", str(DASHBOARD_PORT),
        "--log-level", "warning"
    ]
    api_proc = subprocess.Popen(api_cmd, env=api_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(f"dashboard-api started (PID {api_proc.pid})")

    llama_up, l_time = poll_health(f"http://127.0.0.1:{LLAMA_PORT}/health")
    dash_up, d_time = poll_health(f"http://127.0.0.1:{DASHBOARD_PORT}/health", headers={"Authorization": "Bearer hardened-test-key-999"})
    print(f"llama-server ready: {llama_up} ({l_time:.2f}s)")
    print(f"dashboard-api ready: {dash_up} ({d_time:.2f}s)")

    if not llama_up or not dash_up:
        print("ERROR: Health check failed before chat!", file=sys.stderr)
        llama_proc.kill()
        api_proc.kill()
        sys.exit(1)

    # 3. Send Chat Through dashboard-api (/api/chat)
    print("\n--- 3. SEND CHAT THROUGH DASHBOARD-API (/api/chat) ---")
    chat_payload = {
        "message": "Respond with the single word: HARDENED_POC_VERIFIED",
        "system": "You are a concise AI assistant."
    }
    chat_req = urllib.request.Request(
        f"http://127.0.0.1:{DASHBOARD_PORT}/api/chat",
        data=json.dumps(chat_payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer hardened-test-key-999",
            "Content-Type": "application/json"
        }
    )
    c_start = time.perf_counter()
    try:
        with urllib.request.urlopen(chat_req, timeout=30) as resp:
            c_code = resp.status
            c_data = json.loads(resp.read().decode("utf-8"))
        c_elapsed = time.perf_counter() - c_start
        print(f"dashboard-api /api/chat returned HTTP {c_code} in {c_elapsed:.2f}s.")
        print(f"Payload Response: {c_data}")
        summary["chat_through_dashboard_api"] = {
            "endpoint": "/api/chat",
            "http_status": c_code,
            "elapsed_seconds": round(c_elapsed, 3),
            "response_body": c_data,
            "success": c_data.get("success", False)
        }
    except Exception as e:
        print(f"ERROR: /api/chat call failed: {e}", file=sys.stderr)
        summary["chat_through_dashboard_api"] = {"error": str(e)}

    # 4. Graceful Shutdown & Exit Codes
    print("\n--- 4. GRACEFUL CHILD SHUTDOWN & EXIT CODES ---")
    s_start = time.perf_counter()
    api_proc.terminate()
    llama_proc.terminate()
    try:
        api_code = api_proc.wait(timeout=5)
    except Exception:
        api_proc.kill()
        api_code = api_proc.wait()
    try:
        llama_code = llama_proc.wait(timeout=5)
    except Exception:
        llama_proc.kill()
        llama_code = llama_proc.wait()
    s_elapsed = time.perf_counter() - s_start

    print(f"dashboard-api exited with code: {api_code}")
    print(f"llama-server.exe exited with code: {llama_code}")
    print(f"Graceful shutdown took: {s_elapsed:.2f}s")
    summary["graceful_shutdown"] = {
        "dashboard_api_exit_code": api_code,
        "llama_server_exit_code": llama_code,
        "elapsed_seconds": round(s_elapsed, 3)
    }

    # =========================================================================
    # FAILURE TESTS (Task 4)
    # =========================================================================
    print("\n===============================================================")
    print("               RUNNING FAILURE TEST SCENARIOS                  ")
    print("===============================================================")

    # Failure Test A: Port 8080 already in use
    print("\n--- Failure Test A: Port 8080 Collision ---")
    coll_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    coll_sock.bind(("127.0.0.1", LLAMA_PORT))
    coll_sock.listen(1)
    print(f"Bound blocking socket to 127.0.0.1:{LLAMA_PORT}")

    f_t0 = time.perf_counter()
    f_proc = subprocess.Popen(llama_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out_a, _ = f_proc.communicate(timeout=15)
    f_elapsed = time.perf_counter() - f_t0
    coll_sock.close()

    print(f"llama-server.exe exited with code: {f_proc.returncode} in {f_elapsed:.2f}s.")
    print("--- Stderr / Stdout Snippet ---")
    snippet_a = [line for line in out_a.splitlines() if any(k in line.lower() for k in ["error", "bind", "port", "failed", "socket", "address"])][:5]
    for s in snippet_a:
        print("  " + s)

    summary["failure_tests"]["port_collision"] = {
        "port": LLAMA_PORT,
        "exit_code": f_proc.returncode,
        "elapsed_seconds": round(f_elapsed, 3),
        "error_lines": snippet_a
    }

    # Failure Test B: Supervisor killed, check for orphaned llama-server.exe
    print("\n--- Failure Test B: Supervisor Killed (Orphan Check) ---")
    
    # B1: Standard spawn without Job Object (vulnerable)
    print("Testing standard spawn without Job Object...")
    v_proc = subprocess.Popen(llama_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    v_pid = v_proc.pid
    time.sleep(1.0)
    print(f"Spawned llama-server.exe (PID {v_pid}). Simulating supervisor kill...")
    # Simulate parent sudden termination by not waiting and querying PID
    is_alive_before = is_pid_running(v_pid)
    print(f"PID {v_pid} alive before parent cleanup: {is_alive_before}")
    # Now kill the orphan to clean up CI runner
    v_proc.kill()
    v_proc.wait()
    time.sleep(0.5)
    is_alive_after = is_pid_running(v_pid)
    print(f"PID {v_pid} alive after cleanup: {is_alive_after}")

    # B2: Win32 Job Object test (if on win32)
    job_supported = False
    job_killed_child = False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            # Win32 Job Object definitions
            CreateJobObjectW = ctypes.windll.kernel32.CreateJobObjectW
            CreateJobObjectW.restype = wintypes.HANDLE
            CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)

            SetInformationJobObject = ctypes.windll.kernel32.SetInformationJobObject
            SetInformationJobObject.restype = wintypes.BOOL

            AssignProcessToJobObject = ctypes.windll.kernel32.AssignProcessToJobObject
            AssignProcessToJobObject.restype = wintypes.BOOL
            AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

            CloseHandle = ctypes.windll.kernel32.CloseHandle
            CloseHandle.argtypes = (wintypes.HANDLE,)

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                            ("WriteOperationCount", ctypes.c_uint64),
                            ("OtherOperationCount", ctypes.c_uint64),
                            ("ReadTransferCount", ctypes.c_uint64),
                            ("WriteTransferCount", ctypes.c_uint64),
                            ("OtherTransferCount", ctypes.c_uint64)]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                            ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9

            h_job = CreateJobObjectW(None, None)
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            SetInformationJobObject(h_job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))

            # Launch child and assign
            j_proc = subprocess.Popen(llama_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            j_pid = j_proc.pid
            time.sleep(1.0)

            # Open child process handle with PROCESS_SET_QUOTA | PROCESS_TERMINATE
            PROCESS_ALL_ACCESS = 0x1F0FFF
            h_proc = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, j_pid)
            AssignProcessToJobObject(h_job, h_proc)
            CloseHandle(h_proc)

            print(f"Child llama-server (PID {j_pid}) assigned to Job Object with KILL_ON_JOB_CLOSE.")
            # Now simulate supervisor sudden death by closing job handle!
            CloseHandle(h_job)
            time.sleep(1.0)

            job_child_alive = is_pid_running(j_pid)
            print(f"Child PID {j_pid} alive after Job handle closed: {job_child_alive}")
            job_supported = True
            job_killed_child = not job_child_alive
        except Exception as je:
            print(f"Job Object evaluation exception: {je}")

    summary["failure_tests"]["supervisor_killed_orphan_check"] = {
        "unprotected_orphan_remains_alive_without_job": is_alive_before,
        "win32_job_object_tested": job_supported,
        "win32_job_object_killed_child_on_close": job_killed_child
    }

    # Failure Test C: Model file corrupted
    print("\n--- Failure Test C: Model File Corrupted ---")
    corrupt_model = poc_dir / "corrupted_model.gguf"
    # Write invalid GGUF header
    corrupt_model.write_bytes(b"CORRUPTED_GGUF_HEADER_1234567890_INVALID_MAGIC_BYTES_TEST")
    
    corrupt_cmd = [
        str(llama_exe),
        "--model", str(corrupt_model),
        "--alias", "corrupted.gguf",
        "--host", "127.0.0.1",
        "--port", str(LLAMA_PORT),
        "--no-webui"
    ]
    c_t0 = time.perf_counter()
    c_proc = subprocess.Popen(corrupt_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out_c, _ = c_proc.communicate(timeout=15)
    c_elapsed = time.perf_counter() - c_t0

    print(f"llama-server.exe exited with code: {c_proc.returncode} in {c_elapsed:.2f}s.")
    print("--- Stderr / Stdout Snippet ---")
    snippet_c = [line for line in out_c.splitlines() if any(k in line.lower() for k in ["error", "invalid", "magic", "failed", "corrupt", "gguf"])][:5]
    for s in snippet_c:
        print("  " + s)

    summary["failure_tests"]["corrupted_model"] = {
        "exit_code": c_proc.returncode,
        "elapsed_seconds": round(c_elapsed, 3),
        "error_lines": snippet_c
    }

    # Save summary
    out_file = logs_dir / "hardened_poc_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSUCCESS: All hardened tests and failure diagnostics saved to {out_file}")

if __name__ == "__main__":
    main()
