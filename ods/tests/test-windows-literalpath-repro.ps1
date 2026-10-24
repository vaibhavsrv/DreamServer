# ============================================================================
# Empirical Reproduction of PowerShell Bracket Path and -LiteralPath Defects
# ============================================================================
$ErrorActionPreference = 'Continue'

Write-Host "==============================================================="
Write-Host "RUNNER ENVIRONMENT INFORMATION"
Write-Host "==============================================================="
Write-Host "PowerShell Version: $($PSVersionTable.PSVersion)"
Write-Host "PSEdition:          $($PSVersionTable.PSEdition)"
Write-Host "OS:                 $([System.Environment]::OSVersion.VersionString)"
Write-Host "Runner Name:        $env:RUNNER_NAME"
Write-Host "Image Version:      $env:ImageVersion"
Write-Host "Image OS:           $env:ImageOS"
Write-Host "==============================================================="

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRoot = (Resolve-Path (Join-Path $root "..")).Path

# ----------------------------------------------------------------------------
# STEP 1: Full Native Windows Install Flow in a Bracketed Directory
# ----------------------------------------------------------------------------
Write-Host "`n--- [STEP 1] Testing Native Windows Install Flow in Bracketed Directory ---"
$bracketInstallDir = Join-Path ([IO.Path]::GetTempPath()) "ods-install[bracket]"
if (Test-Path -LiteralPath $bracketInstallDir) {
    Remove-Item -LiteralPath $bracketInstallDir -Recurse -Force
}

$installerScript = Join-Path $root "installers\windows\install-windows.ps1"
$psExe = if ($PSVersionTable.PSEdition -eq "Core") { "pwsh.exe" } else { "powershell.exe" }

Write-Host "Attempting install-windows.ps1 with -InstallDir '$bracketInstallDir' -NonInteractive via $psExe..."
$installOutput = & $psExe -ExecutionPolicy Bypass -File $installerScript -InstallDir $bracketInstallDir -NonInteractive 2>&1
$installExit = $LASTEXITCODE

Write-Host "Installer Exit Code: $installExit"
Write-Host "Installer Output (first 40 lines):"
$installOutput | Select-Object -First 40 | ForEach-Object { Write-Host "  $_" }

# Determine which step/line failed first
$failedFirst = "UNKNOWN"
foreach ($outLine in $installOutput) {
    $lineStr = "$outLine"
    if ($lineStr -match "ODS_INSTALL_ABORTED" -or $lineStr -match "ERROR" -or $lineStr -match "Cannot find path" -or $lineStr -match "failed" -or $lineStr -match "Docker Desktop is not running") {
        $failedFirst = $lineStr
        break
    }
}
Write-Host "FIRST FAILURE IN NATIVE INSTALL: $failedFirst"

# Clean up if directory was created
if (Test-Path -LiteralPath $bracketInstallDir) {
    Remove-Item -LiteralPath $bracketInstallDir -Recurse -Force
}

# ----------------------------------------------------------------------------
# STEP 2: Reproduction of Destructive Commands Without -LiteralPath
# ----------------------------------------------------------------------------
Write-Host "`n--- [STEP 2] Testing Destructive Calls on Bracketed / Wildcard Paths ---"

# Repro 2A: Remove-Item $modelPath -Force (install-windows.ps1:364, 401)
Write-Host "`n[Repro 2A] Remove-Item `$modelPath -Force without -LiteralPath (install-windows.ps1:364, 401)"
$fixtureDir = Join-Path ([IO.Path]::GetTempPath()) "test-models[dir]\data\models"
$null = New-Item -ItemType Directory -Path $fixtureDir -Force
$modelFile = Join-Path $fixtureDir "test-model-q4.gguf"
[IO.File]::WriteAllText($modelFile, "DUMMY_CORRUPT_MODEL_BYTES")

Write-Host "Created file: $modelFile"
Write-Host "File exists before Remove-Item: $(Test-Path -LiteralPath $modelFile)"

try {
    Remove-Item $modelFile -Force -ErrorAction Stop
    Write-Host "Remove-Item returned without error."
} catch {
    Write-Host "Remove-Item threw exception: $($_.Exception.Message)"
}

$stillExists = Test-Path -LiteralPath $modelFile
Write-Host "File exists AFTER Remove-Item (without -LiteralPath): $stillExists"
if ($stillExists) {
    Write-Host "REPRODUCED: Remove-Item without -LiteralPath SILENTLY FAILED TO DELETE FILE because [dir] was treated as wildcard pattern!"
} else {
    Write-Host "UNEXPECTED: File was deleted."
}

# Clean up fixture
Remove-Item -LiteralPath (Join-Path ([IO.Path]::GetTempPath()) "test-models[dir]") -Recurse -Force

# Repro 2B: Remove-Item $script:INFERENCE_PID_FILE (ods.ps1:1989-1990)
Write-Host "`n[Repro 2B] Remove-Item `$script:INFERENCE_PID_FILE without -LiteralPath (ods.ps1:1989-1990)"
$pidFixtureDir = Join-Path ([IO.Path]::GetTempPath()) "test-pid[bracket]\data"
$null = New-Item -ItemType Directory -Path $pidFixtureDir -Force
$pidFile = Join-Path $pidFixtureDir "llama-server.pid"
[IO.File]::WriteAllText($pidFile, "99999")

Write-Host "Created pid file: $pidFile"
Write-Host "PID file exists before: $(Test-Path -LiteralPath $pidFile)"

$testPathResult = Test-Path $pidFile
Write-Host "Test-Path without -LiteralPath result: $testPathResult"

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
$pidStillExists = Test-Path -LiteralPath $pidFile
Write-Host "PID file exists AFTER Remove-Item: $pidStillExists"
if ($pidStillExists) {
    Write-Host "REPRODUCED: Stale PID file was NOT deleted! Left orphan PID file on disk."
}

Remove-Item -LiteralPath (Join-Path ([IO.Path]::GetTempPath()) "test-pid[bracket]") -Recurse -Force

# Repro 2C: Move-Item -Path $partFile -Destination $Destination (ui.ps1:427)
Write-Host "`n[Repro 2C] Move-Item -Path `$partFile without -LiteralPath (ui.ps1:427)"
$dlDir = Join-Path ([IO.Path]::GetTempPath()) "test-dl[bracket]"
$null = New-Item -ItemType Directory -Path $dlDir -Force
$dest = Join-Path $dlDir "final-model.gguf"
$part = "$dest.part"
[IO.File]::WriteAllText($part, "PARTIAL_DOWNLOAD_CONTENT")

Write-Host "Created part file: $part"
Write-Host "Test-Path on part file without -LiteralPath: $(Test-Path $part)"

$moveError = $null
try {
    Move-Item -Path $part -Destination $dest -Force -ErrorAction Stop
} catch {
    $moveError = $_.Exception.Message
}
Write-Host "Move-Item -Path result: Error = $moveError"
Write-Host "Destination exists: $(Test-Path -LiteralPath $dest)"
if ($moveError -or -not (Test-Path -LiteralPath $dest)) {
    Write-Host "REPRODUCED: Move-Item without -LiteralPath failed on bracketed path!"
}

Remove-Item -LiteralPath $dlDir -Recurse -Force

# Repro 2D: Expand-ODSArchive Remove-Item $DestinationPath (ui.ps1:539)
Write-Host "`n[Repro 2D] Expand-ODSArchive Remove-Item `$DestinationPath without -LiteralPath (ui.ps1:539)"
$archiveDest = Join-Path ([IO.Path]::GetTempPath()) "test-archive[extract]\extracted"
$null = New-Item -ItemType Directory -Path $archiveDest -Force
$dummyFile = Join-Path $archiveDest "old-stale.txt"
[IO.File]::WriteAllText($dummyFile, "STALE_DATA")
Write-Host "Created directory to clean: $archiveDest"
try {
    Remove-Item -Path $archiveDest -Recurse -Force -ErrorAction Stop
} catch {
    Write-Host "Remove-Item -Path threw exception: $($_.Exception.Message)"
}
$dirStillExists = Test-Path -LiteralPath $archiveDest
Write-Host "Directory exists AFTER Remove-Item -Path: $dirStillExists"
if ($dirStillExists) {
    Write-Host "REPRODUCED: Remove-Item -Path without -LiteralPath failed to remove existing directory on bracketed path!"
}
Remove-Item -LiteralPath (Join-Path ([IO.Path]::GetTempPath()) "test-archive[extract]") -Recurse -Force

# ----------------------------------------------------------------------------
# STEP 3: Reproduction of ods.ps1 Test-Install and Test-ODSInstallFiles Gate (ods.ps1:101, 3310)
# ----------------------------------------------------------------------------
Write-Host "`n--- [STEP 3] Testing ods.ps1 Test-Install Gate on Bracketed Path ---"
$odsDir = Join-Path ([IO.Path]::GetTempPath()) "my-ods[prod]"
$null = New-Item -ItemType Directory -Path $odsDir -Force

$testPathUnpatched = Test-Path $odsDir
Write-Host "Test-Path '$odsDir' (unpatched without -LiteralPath): $testPathUnpatched"
$testPathPatched = Test-Path -LiteralPath $odsDir
Write-Host "Test-Path -LiteralPath '$odsDir' (patched):             $testPathPatched"

if (-not $testPathUnpatched -and $testPathPatched) {
    Write-Host "REPRODUCED: Test-Install falsely reports ODS is NOT found when install dir contains brackets!"
}

# Also test docker-compose file check
$composeFile = Join-Path $odsDir "docker-compose.base.yml"
[IO.File]::WriteAllText($composeFile, "services: {}")
$composeUnpatched = Test-Path $composeFile
$composePatched = Test-Path -LiteralPath $composeFile
Write-Host "Test-Path '$composeFile' unpatched: $composeUnpatched"
Write-Host "Test-Path -LiteralPath '$composeFile' patched: $composePatched"
if (-not $composeUnpatched -and $composePatched) {
    Write-Host "REPRODUCED: docker-compose.base.yml falsely reported as missing when path contains brackets!"
}

Remove-Item -LiteralPath $odsDir -Recurse -Force

# ----------------------------------------------------------------------------
# STEP 4: Test-WindowsLlmModelReadiness Bracket Check (lib/llm-endpoint.ps1:251)
# ----------------------------------------------------------------------------
Write-Host "`n--- [STEP 4] Testing Test-WindowsLlmModelReadiness Bracket Check ---"
. (Join-Path $root "installers\windows\lib\llm-endpoint.ps1")
$modelTestRoot = Join-Path ([IO.Path]::GetTempPath()) "ods-readiness[bracket]"
$modelsDir = Join-Path (Join-Path $modelTestRoot "data") "models"
$null = New-Item -ItemType Directory -Path $modelsDir -Force
$modelGguf = Join-Path $modelsDir "test-model.gguf"
[IO.File]::WriteAllText($modelGguf, "MOCK_GGUF")

$fakeEndpoint = @{ ChatCompletionsUrl = "http://127.0.0.1:99999/v1/chat/completions" }
$readinessResult = Test-WindowsLlmModelReadiness -Endpoint $fakeEndpoint -InstallDir $modelTestRoot -GgufFile "test-model.gguf" -TimeoutSec 1

Write-Host "Test-WindowsLlmModelReadiness FileExists with bracketed InstallDir: $($readinessResult.FileExists)"
if (-not $readinessResult.FileExists) {
    Write-Host "REPRODUCED: Test-WindowsLlmModelReadiness reported FileExists=False on existing model because InstallDir contains brackets!"
}

Remove-Item -LiteralPath $modelTestRoot -Recurse -Force

Write-Host "`n==============================================================="
Write-Host "REPRODUCTION SUITE COMPLETE"
Write-Host "==============================================================="
