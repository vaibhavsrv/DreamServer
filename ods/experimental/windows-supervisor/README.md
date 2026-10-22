# Experimental Windows Native Supervisor for ODS

This directory contains an experimental, opt-in native process supervisor for running ODS on Windows without Docker or WSL.

## Architecture

Unlike containerized installations or external service wrappers, this supervisor manages the core inference runtime (`llama-server.exe`) and the control plane (`dashboard-api`) as supervised native child processes under a single **Windows NT Job Object**:

1. **Win32 Job Object Supervision**:
   - Initializes a Win32 Job Object with limit flag `0x2000` (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
   - Assigns both `llama-server.exe` and `uvicorn` (dashboard-api) to the same job object.
   - If the supervisor process crashes, is killed (`taskkill /F`), or receives an unexpected exit, the Windows NT kernel immediately and atomically terminates all child processes. Zero orphaned processes or held socket listeners can linger.

2. **Cryptographic Pinned Verification**:
   - Pinned llama.cpp runtime: b9014 CPU x64 (`3503cc30154266ac0fbedd38e39e35a313a13412415738ae8f9e23e4fcb44da2`).
   - Pinned model: SmolLM2 135M Instruct Q4_K_M (`ed5fa30c487b282ec156c29062f1222e5c20875a944ac98289dbd242e947f747`, commit `1bf3e0f9b37c0f13f1737e1ae8c9a3bf8ba405f6`).
   - Refuses startup with exit code 2 on any hash or size mismatch.

3. **Pre-flight & Failure Safeguards**:
   - Pre-flight socket checks prevent port collisions (exits code 3).
   - Magic byte validation prevents memory allocation on corrupted GGUFs (exits code 4).
   - Premature child death during startup is detected with exit code 5.
   - Mid-run child termination triggers supervisor shutdown with exit code 6.

## Running

```bash
# Run probe mode (starts, verifies health & chat completion through dashboard-api, then stops cleanly):
python ods/experimental/windows-supervisor/supervisor.py --run-probe

# Run persistent supervision:
python ods/experimental/windows-supervisor/supervisor.py
```

## Running Tests

```bash
python ods/experimental/windows-supervisor/test_supervisor.py
```

## Known Limitations

- **Host Python Required**: Requires a pre-installed Python 3.11 or 3.12 environment on the Windows host.
- **CPU Inference Only**: Pinned to the CPU backend (`llama-b9014-bin-win-cpu-x64.zip`). Physical GPU acceleration (Vulkan, DirectML, CUDA) requires real hardware qualification.
- **Single-Machine Loopback**: Binds strictly to `127.0.0.1`.
- **Experimental Boundary**: Operates in isolation under `ods/experimental/windows-supervisor/` without altering default ODS production installer or Compose flows.
