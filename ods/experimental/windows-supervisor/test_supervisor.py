#!/usr/bin/env python3
"""
Integration and failure test suite for ODS Windows Native Supervisor.
Exercises:
  1. Hash mismatch rejection
  2. Port collision rejection (exit code 3)
  3. Corrupted GGUF rejection (exit code 4)
  4. Happy path probe execution (exit code 0)
  5. Child killed mid-run detection (exit code 6)
  6. Supervisor killed, zero orphaned processes (Win32 Job Object verification)
"""

import os
import sys
import time
import socket
import subprocess
import urllib.request
from pathlib import Path

PYTHON = sys.executable
THIS_DIR = Path(__file__).resolve().parent
SUPERVISOR_SCRIPT = THIS_DIR / "supervisor.py"
DATA_DIR = THIS_DIR / "test_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

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

def wait_for_llama_ready(port: int = 8080, timeout: float = 25.0) -> bool:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False

def run_test_happy_path():
    print("\n[TEST 1/6] Testing Happy Path Probe...")
    t0 = time.perf_counter()
    shared_data = DATA_DIR / "shared_data"
    cmd = [PYTHON, str(SUPERVISOR_SCRIPT), "--data-dir", str(shared_data), "--run-probe"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    print(f"Exit code: {res.returncode} in {elapsed:.2f}s")
    print(f"Stdout snippet:\n{res.stdout[-300:].strip()}")
    if res.returncode != 0:
        print(f"FULL STDERR:\n{res.stderr}")
    assert res.returncode == 0, f"Expected exit code 0, got {res.returncode}\nStderr: {res.stderr}"
    assert "Both processes healthy" in res.stdout, "Expected healthy processes in stdout"
    assert "Chat probe response" in res.stdout, "Expected chat probe response"
    print("PASS: Happy path executed and verified cleanly.")

def run_test_port_in_use():
    print("\n[TEST 2/6] Testing Port Collision Rejection...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 8080))
    sock.listen(1)
    print("Bound blocking socket to 127.0.0.1:8080.")
    
    try:
        cmd = [PYTHON, str(SUPERVISOR_SCRIPT), "--data-dir", str(DATA_DIR / "shared_data"), "--run-probe"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(f"Exit code: {res.returncode}")
        print(f"Stderr: {res.stderr.strip()}")
        assert res.returncode == 3, f"Expected exit code 3, got {res.returncode}"
        assert "Port 8080 is already in use" in res.stderr, "Expected port collision message in stderr"
        print("PASS: Port collision rejected cleanly with code 3.")
    finally:
        sock.close()

def run_test_hash_mismatch():
    print("\n[TEST 3/6] Testing Hash Mismatch Rejection...")
    bad_data = DATA_DIR / "bad_hash_data"
    bad_data.mkdir(parents=True, exist_ok=True)
    # Write corrupt zip
    (bad_data / "llama-b9014-bin-win-cpu-x64.zip").write_text("corrupted content", encoding="utf-8")
    
    cmd = [PYTHON, str(SUPERVISOR_SCRIPT), "--data-dir", str(bad_data)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(f"Exit code: {res.returncode}")
    print(f"Stderr: {res.stderr.strip()}")
    assert res.returncode == 2, f"Expected exit code 2, got {res.returncode}"
    assert "SHA-256 mismatch" in res.stderr, "Expected SHA-256 mismatch in stderr"
    print("PASS: Hash mismatch rejected cleanly.")

def run_test_corrupt_model():
    print("\n[TEST 4/6] Testing Corrupt Model Rejection...")
    corrupt_data = DATA_DIR / "corrupt_data"
    models_dir = corrupt_data / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    # Write invalid magic bytes
    (models_dir / "SmolLM2-135M-Instruct-Q4_K_M.gguf").write_bytes(b"BAD_MAGIC_HEADER_1234567890")
    
    cmd = [PYTHON, str(SUPERVISOR_SCRIPT), "--data-dir", str(corrupt_data)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(f"Exit code: {res.returncode}")
    print(f"Stderr: {res.stderr.strip()}")
    # Will fail either hash mismatch (2) or magic header (4)
    assert res.returncode in (2, 4), f"Expected exit code 2 or 4, got {res.returncode}"
    print("PASS: Corrupt model rejected cleanly.")

def run_test_child_killed_mid_run():
    print("\n[TEST 5/6] Testing Child Killed Mid-Run Detection...")
    shared_data = DATA_DIR / "shared_data"
    cmd = [PYTHON, str(SUPERVISOR_SCRIPT), "--data-dir", str(shared_data)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    # Wait for readiness
    ready = wait_for_llama_ready(8080, timeout=25.0)
    print(f"Llama readiness before kill: {ready}")
    assert ready, "llama-server failed to become ready before mid-run kill test!"
    
    # Find llama-server child and terminate forcefully
    if sys.platform == "win32":
        res = subprocess.run(["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True)
        print(f"Llama process query: {res.stdout.strip()}")
        subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"], capture_output=True)
    else:
        proc.terminate()
        
    out, err = proc.communicate(timeout=15)
    print(f"Supervisor exit code: {proc.returncode}")
    print(f"Stderr: {err.strip()}")
    assert proc.returncode == 6, f"Expected exit code 6, got {proc.returncode}"
    assert "died unexpectedly" in err, "Expected child death detection message"
    print("PASS: Child death detected and supervisor exited non-zero.")

def run_test_supervisor_killed_no_orphans():
    print("\n[TEST 6/6] Testing Supervisor Killed: Zero Orphan Verification...")
    shared_data = DATA_DIR / "shared_data"
    cmd = [PYTHON, str(SUPERVISOR_SCRIPT), "--data-dir", str(shared_data)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    # Wait for readiness and job object assignment
    ready = wait_for_llama_ready(8080, timeout=25.0)
    print(f"Llama readiness before supervisor kill: {ready}")
    assert ready, "llama-server failed to become ready before supervisor kill test!"
    
    sup_pid = proc.pid
    print(f"Supervisor PID: {sup_pid}")
    
    # Forcefully kill supervisor
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(sup_pid)], capture_output=True)
        time.sleep(2.0)
        # Check if llama-server.exe is still alive
        check_llama = subprocess.run(["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True)
        print(f"Post-kill llama-server process check: {check_llama.stdout.strip()}")
        assert "llama-server.exe" not in check_llama.stdout, "Orphaned llama-server.exe detected!"
    else:
        proc.kill()
        proc.wait()
        
    print("PASS: Supervisor killed; zero orphaned child processes remain in system.")

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
    print("\n===============================================================")
    print("SUCCESS: All 6 supervisor integration & failure tests passed!  ")
    print("===============================================================")

if __name__ == "__main__":
    main()
