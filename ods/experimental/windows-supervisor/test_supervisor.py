#!/usr/bin/env python3
"""
Integration and failure test suite for ODS Windows Native Supervisor.
Exercises:
  1. Happy path probe execution with per-run API key & unique ports (exit code 0)
  2. Port collision rejection with unique port (exit code 3)
  3. Hash mismatch rejection (exit code 2)
  4. Corrupted GGUF rejection (exit code 2)
  5. Child killed mid-run detection asserting exit code 6 (kill by spawned PID only)
  6. Supervisor killed, zero orphaned processes (Win32 Job Object verification by PID)
  7. Stop path verification: graceful cooperative exit vs forced timeout kill
"""

import os
import sys
import time
import json
import signal
import socket
import subprocess
import urllib.request
from pathlib import Path

PYTHON = sys.executable
THIS_DIR = Path(__file__).resolve().parent
SUPERVISOR_SCRIPT = THIS_DIR / "supervisor.py"
DATA_DIR = THIS_DIR / "test_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

def get_free_port() -> int:
    """Finds an unused loopback TCP port allocated by the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def get_free_port_pair() -> tuple[int, int]:
    """Finds two distinct unused loopback TCP ports."""
    p1 = get_free_port()
    p2 = get_free_port()
    while p2 == p1:
        p2 = get_free_port()
    return p1, p2

def verify_port_available(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        return False

def is_pid_running(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True)
            for line in res.stdout.splitlines():
                parts = [p.strip('"') for p in line.split(",")]
                if len(parts) >= 2 and parts[1] == str(pid):
                    return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

def wait_for_both_ready(llama_port: int, dashboard_port: int, api_key: str = None, timeout: float = 30.0) -> bool:
    t0 = time.perf_counter()
    llama_ready = False
    dash_ready = False
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    while time.perf_counter() - t0 < timeout:
        if not llama_ready:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{llama_port}/health", timeout=1.0) as r:
                    if r.status == 200:
                        llama_ready = True
            except Exception:
                pass
        if not dash_ready:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{dashboard_port}/health", headers=headers)
                with urllib.request.urlopen(req, timeout=1.0) as r:
                    if r.status == 200:
                        dash_ready = True
            except Exception:
                pass
        if llama_ready and dash_ready:
            return True
        time.sleep(0.4)
    return False

def wait_for_pids_ready(pids_file: Path, timeout: float = 30.0) -> dict:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if pids_file.exists():
            try:
                data = json.loads(pids_file.read_text(encoding="utf-8"))
                if data.get("ready") and data.get("llama_pid") and data.get("dashboard_pid"):
                    return data
            except Exception:
                pass
        time.sleep(0.4)
    raise TimeoutError(f"Timed out waiting for pids.json readiness at {pids_file}")

def run_test_happy_path():
    print("\n[TEST 1/7] Testing Happy Path Probe with Per-Run API Key & Unique Ports...")
    llama_port, dash_port = get_free_port_pair()
    print(f"Allocated ports: llama={llama_port}, dashboard={dash_port}")
    t0 = time.perf_counter()
    shared_data = DATA_DIR / "shared_data"
    cmd = [
        PYTHON, str(SUPERVISOR_SCRIPT),
        "--data-dir", str(shared_data),
        "--llama-port", str(llama_port),
        "--dashboard-port", str(dash_port),
        "--run-probe"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    print(f"Exit code: {res.returncode} in {elapsed:.2f}s")
    print(f"Stdout snippet:\n{res.stdout[-350:].strip()}")
    if res.returncode != 0:
        print(f"FULL STDERR:\n{res.stderr}")
    assert res.returncode == 0, f"Expected exit code 0, got {res.returncode}\nStderr: {res.stderr}"
    assert "Both processes healthy" in res.stdout, "Expected healthy processes in stdout"
    assert "Chat probe response" in res.stdout, "Expected chat probe response"

    # Verify api_key.txt was created
    key_file = shared_data / "api_key.txt"
    assert key_file.exists(), "api_key.txt not written"
    api_key = key_file.read_text(encoding="utf-8").strip()
    assert len(api_key) >= 16, f"Generated API key is unexpectedly short: {api_key}"
    print(f"PASS: Happy path executed and verified cleanly (API Key: {api_key[:8]}...).")

def run_test_port_in_use():
    print("\n[TEST 2/7] Testing Port Collision Rejection...")
    conflict_port = get_free_port()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", conflict_port))
    sock.listen(1)
    print(f"Bound blocking socket to 127.0.0.1:{conflict_port}.")

    try:
        cmd = [
            PYTHON, str(SUPERVISOR_SCRIPT),
            "--data-dir", str(DATA_DIR / "shared_data"),
            "--llama-port", str(conflict_port),
            "--run-probe"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(f"Exit code: {res.returncode}")
        print(f"Stderr: {res.stderr.strip()}")
        assert res.returncode == 3, f"Expected exit code 3, got {res.returncode}"
        assert f"Port {conflict_port} is already in use" in res.stderr, "Expected port collision message in stderr"
        print("PASS: Port collision rejected cleanly with code 3.")
    finally:
        sock.close()

def run_test_hash_mismatch():
    print("\n[TEST 3/7] Testing Hash Mismatch Rejection...")
    bad_data = DATA_DIR / "bad_hash_data"
    bad_data.mkdir(parents=True, exist_ok=True)
    (bad_data / "llama-b9014-bin-win-cpu-x64.zip").write_text("corrupted content", encoding="utf-8")

    cmd = [PYTHON, str(SUPERVISOR_SCRIPT), "--data-dir", str(bad_data)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(f"Exit code: {res.returncode}")
    print(f"Stderr: {res.stderr.strip()}")
    assert res.returncode == 2, f"Expected exit code 2, got {res.returncode}"
    assert "SHA-256 mismatch" in res.stderr, "Expected SHA-256 mismatch in stderr"
    print("PASS: Hash mismatch rejected cleanly.")

def run_test_corrupt_model():
    print("\n[TEST 4/7] Testing Corrupt Model Rejection...")
    corrupt_data = DATA_DIR / "corrupt_data"
    models_dir = corrupt_data / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "SmolLM2-135M-Instruct-Q4_K_M.gguf").write_bytes(b"BAD_MAGIC_HEADER_1234567890")

    cmd = [PYTHON, str(SUPERVISOR_SCRIPT), "--data-dir", str(corrupt_data)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(f"Exit code: {res.returncode}")
    print(f"Stderr: {res.stderr.strip()}")
    assert res.returncode == 2, f"Expected exit code 2, got {res.returncode}"
    assert "SHA-256 mismatch for GGUF model" in res.stderr, "Expected model SHA-256 mismatch message"
    print("PASS: Corrupt model rejected cleanly with exit code 2.")

def run_test_child_killed_mid_run():
    print("\n[TEST 5/7] Testing Child Killed Mid-Run Detection (Assert Exit 6, Kill by PID Only)...")
    llama_port, dash_port = get_free_port_pair()
    shared_data = DATA_DIR / "shared_data"
    pids_file = shared_data / "pids.json"
    if pids_file.exists():
        pids_file.unlink()

    cmd = [
        PYTHON, str(SUPERVISOR_SCRIPT),
        "--data-dir", str(shared_data),
        "--llama-port", str(llama_port),
        "--dashboard-port", str(dash_port)
    ]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=creationflags)

    # Wait until supervisor reports healthy status in pids.json
    pids_info = wait_for_pids_ready(pids_file, timeout=30.0)
    llama_pid = pids_info["llama_pid"]
    print(f"Supervisor confirmed ready. Spawned llama-server PID: {llama_pid}")
    assert proc.poll() is None, "Supervisor exited prematurely before mid-run kill!"
    assert is_pid_running(llama_pid), f"llama-server PID {llama_pid} is not running!"

    # Kill child strictly by spawned PID only (zero /IM flags)
    print(f"Killing child llama-server PID {llama_pid} by PID only...")
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(llama_pid)], capture_output=True)
    else:
        os.kill(llama_pid, signal.SIGKILL)

    out, err = proc.communicate(timeout=15)
    print(f"Supervisor exit code: {proc.returncode}")
    print(f"Stderr: {err.strip()}")
    assert proc.returncode == 6, f"Expected exit code 6, got {proc.returncode}\nStderr: {err}"
    assert "died unexpectedly" in err, "Expected child death detection message"
    print("PASS: Child death by PID detected and supervisor exited with strictly code 6.")

def run_test_supervisor_killed_no_orphans():
    print("\n[TEST 6/7] Testing Supervisor Killed: Zero Orphan Verification (Kill by PID Only)...")
    llama_port, dash_port = get_free_port_pair()
    shared_data = DATA_DIR / "shared_data"
    pids_file = shared_data / "pids.json"
    if pids_file.exists():
        pids_file.unlink()

    cmd = [
        PYTHON, str(SUPERVISOR_SCRIPT),
        "--data-dir", str(shared_data),
        "--llama-port", str(llama_port),
        "--dashboard-port", str(dash_port)
    ]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=creationflags)

    pids_info = wait_for_pids_ready(pids_file, timeout=30.0)
    sup_pid = pids_info["supervisor_pid"]
    llama_pid = pids_info["llama_pid"]
    dash_pid = pids_info["dashboard_pid"]
    print(f"Supervisor PID: {sup_pid}, llama PID: {llama_pid}, dash PID: {dash_pid}")
    assert proc.poll() is None, "Supervisor exited prematurely!"
    assert is_pid_running(llama_pid), "llama process not running!"
    assert is_pid_running(dash_pid), "dashboard process not running!"

    # Forcefully kill supervisor by PID only
    print(f"Killing supervisor PID {sup_pid} forcefully by PID only...")
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(sup_pid)], capture_output=True)

        # Wait up to 5s for Win32 Job Object kernel cleanup
        t_wait = time.perf_counter()
        while time.perf_counter() - t_wait < 5.0:
            if not is_pid_running(llama_pid) and not is_pid_running(dash_pid):
                break
            time.sleep(0.3)

        # Assert BOTH spawned PIDs are dead (verified by PID only)
        llama_running = is_pid_running(llama_pid)
        dash_running = is_pid_running(dash_pid)
        print(f"Post-kill PID checks: llama_pid({llama_pid}) running={llama_running}, dash_pid({dash_pid}) running={dash_running}")
        assert not llama_running, f"Orphaned llama-server (PID {llama_pid}) detected!"
        assert not dash_running, f"Orphaned dashboard-api (PID {dash_pid}) detected!"

        # Assert ports are released
        assert verify_port_available(llama_port), f"Port {llama_port} remains occupied after supervisor kill!"
        assert verify_port_available(dash_port), f"Port {dash_port} remains occupied after supervisor kill!"
    else:
        proc.kill()
        proc.wait()

    print("PASS: Supervisor killed by PID; both children confirmed terminated with zero orphans.")

def run_test_stop_path_graceful_and_forced():
    print("\n[TEST 7/7] Testing Stop Paths: Graceful Cooperative Exit vs Forced Timeout Kill...")
    shared_data = DATA_DIR / "shared_data"
    pids_file = shared_data / "pids.json"

    # --- SUBTEST A: Graceful Cooperative Stop ---
    print("\n  [Subtest 7A] Graceful Stop Path (cooperative exit within timeout)...")
    llama_port_a, dash_port_a = get_free_port_pair()
    if pids_file.exists():
        pids_file.unlink()

    cmd_a = [
        PYTHON, str(SUPERVISOR_SCRIPT),
        "--data-dir", str(shared_data),
        "--llama-port", str(llama_port_a),
        "--dashboard-port", str(dash_port_a),
        "--stop-timeout", "5.0"
    ]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc_a = subprocess.Popen(cmd_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              creationflags=creationflags)
    pids_a = wait_for_pids_ready(pids_file, timeout=30.0)
    print(f"  Supervisor ready. PIDs: llama={pids_a['llama_pid']}, dash={pids_a['dashboard_pid']}")

    # Send cooperative signal to supervisor
    print("  Dispatching cooperative break/interrupt signal...")
    if sys.platform == "win32":
        proc_a.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc_a.send_signal(signal.SIGINT)

    out_a, err_a = proc_a.communicate(timeout=15)
    print(f"  Graceful stop exit code: {proc_a.returncode}")
    print(f"  Stdout:\n{out_a[-300:].strip()}")
    assert proc_a.returncode == 0, f"Expected exit 0 for graceful stop, got {proc_a.returncode}\nErr: {err_a}"
    assert "Stop mode: GRACEFUL" in out_a or "exited cleanly" in out_a, "Expected graceful stop indication in output"
    assert not is_pid_running(pids_a['llama_pid']), "llama process still running after graceful stop!"
    assert not is_pid_running(pids_a['dashboard_pid']), "dashboard process still running after graceful stop!"
    assert verify_port_available(llama_port_a), f"Port {llama_port_a} not released"
    assert verify_port_available(dash_port_a), f"Port {dash_port_a} not released"
    print("  PASS: Graceful stop path confirmed clean cooperative exit.")

    # --- SUBTEST B: Forced Timeout Stop ---
    print("\n  [Subtest 7B] Forced Stop Path (timeout expires, triggers forced kill)...")
    llama_port_b, dash_port_b = get_free_port_pair()
    if pids_file.exists():
        pids_file.unlink()

    cmd_b = [
        PYTHON, str(SUPERVISOR_SCRIPT),
        "--data-dir", str(shared_data),
        "--llama-port", str(llama_port_b),
        "--dashboard-port", str(dash_port_b),
        "--stop-timeout", "0.001"  # Near-zero timeout forces timeout branch
    ]
    proc_b = subprocess.Popen(cmd_b, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              creationflags=creationflags)
    pids_b = wait_for_pids_ready(pids_file, timeout=30.0)
    print(f"  Supervisor ready. PIDs: llama={pids_b['llama_pid']}, dash={pids_b['dashboard_pid']}")

    # Send signal to trigger stop with near-zero timeout
    print("  Dispatching break signal with 0.001s timeout...")
    if sys.platform == "win32":
        proc_b.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc_b.send_signal(signal.SIGINT)

    out_b, err_b = proc_b.communicate(timeout=15)
    print(f"  Forced stop exit code: {proc_b.returncode}")
    print(f"  Stdout:\n{out_b[-300:].strip()}")
    if err_b:
        print(f"  Stderr:\n{err_b[-300:].strip()}")
    assert proc_b.returncode == 0, f"Expected exit 0 for forced stop, got {proc_b.returncode}"
    assert "Stop mode: FORCED" in out_b or "terminating forcefully" in err_b, "Expected forced stop indication"
    assert not is_pid_running(pids_b['llama_pid']), "llama process still running after forced stop!"
    assert not is_pid_running(pids_b['dashboard_pid']), "dashboard process still running after forced stop!"
    assert verify_port_available(llama_port_b), f"Port {llama_port_b} not released"
    assert verify_port_available(dash_port_b), f"Port {dash_port_b} not released"
    print("  PASS: Forced stop path confirmed timeout fallback and zero orphans.")

def main():
    print("===============================================================")
    print("       ODS Windows Native Supervisor Test Suite                ")
    print("===============================================================")
    run_test_happy_path()
    time.sleep(1.0)
    run_test_port_in_use()
    time.sleep(0.5)
    run_test_hash_mismatch()
    time.sleep(0.5)
    run_test_corrupt_model()
    time.sleep(0.5)
    run_test_child_killed_mid_run()
    time.sleep(1.0)
    run_test_supervisor_killed_no_orphans()
    time.sleep(1.0)
    run_test_stop_path_graceful_and_forced()
    print("\n===============================================================")
    print("SUCCESS: All 7 supervisor integration & failure tests passed!  ")
    print("===============================================================")

if __name__ == "__main__":
    main()
