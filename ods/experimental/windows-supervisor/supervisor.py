#!/usr/bin/env python3
"""
Experimental Opt-In Windows Native Supervisor for ODS
Supervises llama-server.exe and dashboard-api inside a single Win32 Job Object
with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, ensuring zero orphaned processes.
"""

import os
import sys
import time
import json
import signal
import socket
import hashlib
import zipfile
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

# --- Pinned Dependency Specifications ---
LLAMA_SERVER_PIN = {
    "release_tag": "b9014",
    "commit": "d4b0c22f9",
    "asset": "llama-b9014-bin-win-cpu-x64.zip",
    "url": "https://github.com/ggml-org/llama.cpp/releases/download/b9014/llama-b9014-bin-win-cpu-x64.zip",
    "sha256": "3503cc30154266ac0fbedd38e39e35a313a13412415738ae8f9e23e4fcb44da2",
    "size_bytes": 15917797,
    "exe_name": "llama-server.exe",
    "exe_sha256": "fe91d802f890c05b97b79fabdf7002abc8be4872f239ef21ff55d7d6c3d8c591"
}

MODEL_PIN = {
    "repo": "unsloth/SmolLM2-135M-Instruct-GGUF",
    "commit": "1bf3e0f9b37c0f13f1737e1ae8c9a3bf8ba405f6",
    "filename": "SmolLM2-135M-Instruct-Q4_K_M.gguf",
    "url": "https://huggingface.co/unsloth/SmolLM2-135M-Instruct-GGUF/resolve/1bf3e0f9b37c0f13f1737e1ae8c9a3bf8ba405f6/SmolLM2-135M-Instruct-Q4_K_M.gguf",
    "sha256": "ed5fa30c487b282ec156c29062f1222e5c20875a944ac98289dbd242e947f747",
    "size_bytes": 105454144
}

# Win32 Constants
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
PROCESS_ALL_ACCESS = 0x1F0FFF

def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()

def verify_port_available(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        return False

def verify_gguf_header(file_path: Path) -> bool:
    if not file_path.is_file() or file_path.stat().st_size < 4:
        return False
    with open(file_path, "rb") as f:
        magic = f.read(4)
        return magic == b"GGUF"

class Win32JobManager:
    """Manages an NT Job Object ensuring all assigned processes terminate when closed."""
    def __init__(self):
        self.handle = None
        self.is_windows = sys.platform == "win32"
        if self.is_windows:
            self._init_job_object()

    def _init_job_object(self):
        import ctypes
        from ctypes import wintypes

        CreateJobObjectW = ctypes.windll.kernel32.CreateJobObjectW
        CreateJobObjectW.restype = wintypes.HANDLE
        CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)

        SetInformationJobObject = ctypes.windll.kernel32.SetInformationJobObject
        SetInformationJobObject.restype = wintypes.BOOL

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

        self.handle = CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeError(f"CreateJobObjectW failed with error {ctypes.GetLastError()}")

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise RuntimeError(f"SetInformationJobObject failed with error {ctypes.GetLastError()}")

    def assign_process(self, pid: int):
        if not self.is_windows or not self.handle:
            return
        import ctypes
        h_proc = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not h_proc:
            raise RuntimeError(f"OpenProcess for PID {pid} failed with error {ctypes.GetLastError()}")
        try:
            ok = ctypes.windll.kernel32.AssignProcessToJobObject(self.handle, h_proc)
            if not ok:
                raise RuntimeError(f"AssignProcessToJobObject failed for PID {pid} with error {ctypes.GetLastError()}")
        finally:
            ctypes.windll.kernel32.CloseHandle(h_proc)

    def close(self):
        if self.handle:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None

class WindowsNativeSupervisor:
    """Supervises llama-server and dashboard-api native execution."""
    def __init__(self, data_dir: Path, llama_port: int = 8080, dashboard_port: int = 3002):
        self.data_dir = data_dir.resolve()
        self.llama_port = llama_port
        self.dashboard_port = dashboard_port
        self.job = Win32JobManager()
        self.llama_proc = None
        self.dashboard_proc = None
        self.running = False
        
        self.runtime_dir = self.data_dir / "runtime"
        self.models_dir = self.data_dir / "models"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def download_and_verify(self, force: bool = False):
        # 1. Download and verify llama-server
        zip_path = self.data_dir / LLAMA_SERVER_PIN["asset"]
        if force or not zip_path.exists() or zip_path.stat().st_size != LLAMA_SERVER_PIN["size_bytes"]:
            print(f"Downloading {LLAMA_SERVER_PIN['url']}...")
            self._download(LLAMA_SERVER_PIN["url"], zip_path)
            
        actual_zip_sha = compute_sha256(zip_path)
        if actual_zip_sha != LLAMA_SERVER_PIN["sha256"]:
            print(f"ERROR: SHA-256 mismatch for runtime archive! Expected {LLAMA_SERVER_PIN['sha256']}, got {actual_zip_sha}", file=sys.stderr)
            sys.exit(2)
            
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(self.runtime_dir)
            
        llama_exe = self.runtime_dir / LLAMA_SERVER_PIN["exe_name"]
        if not llama_exe.exists():
            matches = list(self.runtime_dir.glob(f"**/{LLAMA_SERVER_PIN['exe_name']}"))
            if not matches:
                print(f"ERROR: {LLAMA_SERVER_PIN['exe_name']} missing from extracted archive!", file=sys.stderr)
                sys.exit(2)
            llama_exe = matches[0]
            
        actual_exe_sha = compute_sha256(llama_exe)
        if actual_exe_sha != LLAMA_SERVER_PIN["exe_sha256"]:
            print(f"ERROR: SHA-256 mismatch for llama-server.exe! Expected {LLAMA_SERVER_PIN['exe_sha256']}, got {actual_exe_sha}", file=sys.stderr)
            sys.exit(2)

        # 2. Download and verify model GGUF
        model_path = self.models_dir / MODEL_PIN["filename"]
        if force or not model_path.exists() or model_path.stat().st_size != MODEL_PIN["size_bytes"]:
            print(f"Downloading {MODEL_PIN['url']}...")
            self._download(MODEL_PIN["url"], model_path)
            
        actual_model_sha = compute_sha256(model_path)
        if actual_model_sha != MODEL_PIN["sha256"]:
            print(f"ERROR: SHA-256 mismatch for GGUF model! Expected {MODEL_PIN['sha256']}, got {actual_model_sha}", file=sys.stderr)
            sys.exit(2)

        if not verify_gguf_header(model_path):
            print(f"ERROR: Model file {model_path} has invalid GGUF magic header!", file=sys.stderr)
            sys.exit(4)

        print("SUCCESS: Runtime and Model verified with matching cryptographic hashes.")
        return llama_exe, model_path

    def _download(self, url: str, dest: Path):
        req = urllib.request.Request(url, headers={"User-Agent": "ODS-Supervisor/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            while chunk := resp.read(1024 * 1024):
                f.write(chunk)

    def start(self, llama_exe: Path, model_path: Path):
        # 1. Pre-flight port checks
        if not verify_port_available(self.llama_port):
            print(f"ERROR: Port {self.llama_port} is already in use by another process.", file=sys.stderr)
            sys.exit(3)
        if not verify_port_available(self.dashboard_port):
            print(f"ERROR: Port {self.dashboard_port} is already in use by another process.", file=sys.stderr)
            sys.exit(3)

        # 2. Verify model header
        if not verify_gguf_header(model_path):
            print(f"ERROR: Model file is corrupted or invalid GGUF format.", file=sys.stderr)
            sys.exit(4)

        # 3. Launch llama-server.exe
        llama_cmd = [
            str(llama_exe),
            "--model", str(model_path),
            "--alias", model_path.name,
            "--host", "127.0.0.1",
            "--port", str(self.llama_port),
            "--ctx-size", "2048",
            "--parallel", "1",
            "--threads", "4",
            "--no-webui"
        ]
        print(f"Starting llama-server.exe on port {self.llama_port}...")
        self.llama_proc = subprocess.Popen(llama_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.job.assign_process(self.llama_proc.pid)
        print(f"Assigned llama-server (PID {self.llama_proc.pid}) to Win32 Job Object.")

        # 4. Launch dashboard-api via Uvicorn
        repo_root = Path(__file__).resolve().parents[3]
        api_dir = repo_root / "ods" / "extensions" / "services" / "dashboard-api"
        env = os.environ.copy()
        env["DASHBOARD_API_KEY"] = "supervisor-native-key"
        env["ODS_INSTALL_DIR"] = str(repo_root / "ods")
        env["ODS_DATA_DIR"] = str(self.data_dir / "ods_data")
        env["ODS_EXTENSIONS_DIR"] = str(repo_root / "ods" / "extensions" / "services")
        env["PYTHONPATH"] = str(api_dir)
        env["SERVICE_HOST"] = "127.0.0.1"
        env["LLM_API_URL"] = f"http://127.0.0.1:{self.llama_port}/v1"
        env["LLM_MODEL"] = model_path.name
        env["GGUF_FILE"] = model_path.name
        env["AMD_INFERENCE_LOCATION"] = ""
        (self.data_dir / "ods_data").mkdir(parents=True, exist_ok=True)

        api_cmd = [
            sys.executable, "-m", "uvicorn", "main:app",
            "--app-dir", str(api_dir),
            "--host", "127.0.0.1",
            "--port", str(self.dashboard_port),
            "--log-level", "warning"
        ]
        print(f"Starting dashboard-api on port {self.dashboard_port}...")
        self.dashboard_proc = subprocess.Popen(api_cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.job.assign_process(self.dashboard_proc.pid)
        print(f"Assigned dashboard-api (PID {self.dashboard_proc.pid}) to Win32 Job Object.")

        # 5. Wait for readiness
        self._wait_for_health()
        self.running = True

    def _wait_for_health(self, timeout: float = 30.0):
        t0 = time.perf_counter()
        llama_url = f"http://127.0.0.1:{self.llama_port}/health"
        dash_url = f"http://127.0.0.1:{self.dashboard_port}/health"
        headers = {"Authorization": "Bearer supervisor-native-key"}

        llama_ready = False
        dash_ready = False

        while time.perf_counter() - t0 < timeout:
            # Check for premature child death
            if self.llama_proc.poll() is not None:
                out = self.llama_proc.stdout.read().decode("utf-8", "replace") if self.llama_proc.stdout else ""
                print(f"ERROR: Child process llama-server.exe exited prematurely with exit code {self.llama_proc.returncode}!\n{out}", file=sys.stderr)
                self.stop()
                sys.exit(5)
            if self.dashboard_proc.poll() is not None:
                out = self.dashboard_proc.stdout.read().decode("utf-8", "replace") if self.dashboard_proc.stdout else ""
                print(f"ERROR: Child process dashboard-api exited prematurely with exit code {self.dashboard_proc.returncode}!\n{out}", file=sys.stderr)
                self.stop()
                sys.exit(5)

            if not llama_ready:
                try:
                    with urllib.request.urlopen(llama_url, timeout=1.0) as r:
                        if r.status == 200:
                            llama_ready = True
                            print(f"llama-server.exe is ready ({time.perf_counter() - t0:.2f}s).")
                except Exception:
                    pass

            if not dash_ready:
                try:
                    req = urllib.request.Request(dash_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=1.0) as r:
                        if r.status == 200:
                            dash_ready = True
                            print(f"dashboard-api is ready ({time.perf_counter() - t0:.2f}s).")
                except Exception:
                    pass

            if llama_ready and dash_ready:
                print(f"SUCCESS: Both processes healthy and responding on loopback in {time.perf_counter() - t0:.2f}s.")
                return

            time.sleep(0.4)

        print(f"ERROR: Timed out waiting for child health after {timeout}s!", file=sys.stderr)
        self.stop()
        sys.exit(5)

    def monitor_until_interrupted(self):
        """Monitors child processes. Exits non-zero if either dies."""
        try:
            while self.running:
                if self.llama_proc and self.llama_proc.poll() is not None:
                    code = self.llama_proc.returncode
                    print(f"ERROR: Child process llama-server.exe (PID {self.llama_proc.pid}) died unexpectedly with exit code {code}.", file=sys.stderr)
                    self.stop()
                    sys.exit(6)
                if self.dashboard_proc and self.dashboard_proc.poll() is not None:
                    code = self.dashboard_proc.returncode
                    print(f"ERROR: Child process dashboard-api (PID {self.dashboard_proc.pid}) died unexpectedly with exit code {code}.", file=sys.stderr)
                    self.stop()
                    sys.exit(6)
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nReceived interrupt. Stopping gracefully...")
            self.stop()
            sys.exit(0)

    def stop(self, timeout: float = 5.0):
        self.running = False
        print("Stopping supervised processes...")
        for name, proc in [("dashboard-api", self.dashboard_proc), ("llama-server", self.llama_proc)]:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except Exception:
                    proc.kill()
                    proc.wait()
        self.job.close()
        print("Win32 Job Object closed; child processes terminated cleanly.")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="ODS Experimental Windows Native Supervisor")
    parser.add_argument("--data-dir", default="./data/supervisor_data", help="Data directory")
    parser.add_argument("--llama-port", type=int, default=8080)
    parser.add_argument("--dashboard-port", type=int, default=3002)
    parser.add_argument("--run-probe", action="store_true", help="Start, verify health & chat, then stop")
    args = parser.parse_args()

    sup = WindowsNativeSupervisor(Path(args.data_dir), args.llama_port, args.dashboard_port)
    
    # Register signal handlers
    def handle_signal(sig, frame):
        sup.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    llama_exe, model_path = sup.download_and_verify()
    sup.start(llama_exe, model_path)

    if args.run_probe:
        # Run a test chat query through dashboard-api
        print("Probing dashboard-api /api/chat...")
        chat_payload = {"message": "Say PASS", "system": "You are a test assistant."}
        req = urllib.request.Request(
            f"http://127.0.0.1:{args.dashboard_port}/api/chat",
            data=json.dumps(chat_payload).encode("utf-8"),
            headers={"Authorization": "Bearer supervisor-native-key", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"Chat probe response: {data}")
            if not data.get("success"):
                sup.stop()
                sys.exit(1)
        sup.stop()
        print("SUCCESS: Probe completed cleanly.")
        sys.exit(0)
    else:
        sup.monitor_until_interrupted()

if __name__ == "__main__":
    main()
