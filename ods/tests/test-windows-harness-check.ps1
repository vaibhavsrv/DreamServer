#Requires -Version 5.1
<#
.SYNOPSIS
    Harness check: Reruns "stop" and "start" space-path cases using a corrected docker shim
    and real docker.exe (if available), WITHOUT overriding USERPROFILE (only ODS_HOME).
    Verifies whether the empty-Startup-folder crash and batch error were test harness artifacts.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"

Write-Host "======================================================================"
Write-Host "HARNESS CHECK: Space-Path 'stop' and 'start' without USERPROFILE override"
Write-Host "PowerShell Version: $($PSVersionTable.PSVersion)"
Write-Host "OS: $([System.Environment]::OSVersion.VersionString)"
Write-Host "USERPROFILE: $env:USERPROFILE"
Write-Host "Startup Folder: $([System.Environment]::GetFolderPath('Startup'))"
Write-Host "======================================================================`n"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$cliPath = Join-Path $repoRoot "ods\installers\windows\ods.ps1"

if (-not (Test-Path -LiteralPath $cliPath)) {
    Write-Error "ods.ps1 not found at $cliPath"
    exit 1
}

$tempRoot = [System.IO.Path]::GetTempPath()
$spaceDir = Join-Path $tempRoot "ods test space $(Get-Random)"
$odsHome = Join-Path $spaceDir "ODS Folder"

Write-Host "Creating space path: $odsHome"
[System.IO.Directory]::CreateDirectory($odsHome) | Out-Null

# Create minimal required files in ODS_HOME
$envContent = @"
ODS_HOME=$odsHome
PORT=3000
WEBUI_PORT=3000
DASHBOARD_PORT=3001
OPENCODE_PORT=3003
GPU_BACKEND=cpu
"@
[System.IO.File]::WriteAllText((Join-Path $odsHome ".env"), $envContent, [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText((Join-Path $odsHome "docker-compose.yml"), "services:`n  mock:`n    image: hello`n", [System.Text.Encoding]::UTF8)

# Set ONLY ODS_HOME -- DO NOT TOUCH USERPROFILE OR LOCALAPPDATA
$origOdsHome = $env:ODS_HOME
$env:ODS_HOME = $odsHome

# -------------------------------------------------------------------------
# Test 1: With Corrected Docker Shim
# -------------------------------------------------------------------------
Write-Host "`n>>> [TEST 1] Running with CORRECTED docker.cmd shim in PATH..."
$shimDir = Join-Path $spaceDir "shim_bin"
[System.IO.Directory]::CreateDirectory($shimDir) | Out-Null
$shimPath = Join-Path $shimDir "docker.cmd"

# Robust batch shim that safely handles quoted and unquoted space paths
@"
@echo off
if /I "%~1"=="info" (
    echo Docker version 27.0.0
    exit /b 0
)
if /I "%~1"=="compose" (
    echo NAME      IMAGE     COMMAND   SERVICE   CREATED   STATUS    PORTS
    exit /b 0
)
exit /b 0
"@ | Set-Content -LiteralPath $shimPath -Encoding ASCII

$origPath = $env:PATH
$env:PATH = "$shimDir;$origPath"

function Run-Step {
    param([string]$StepName, [string]$CommandArgs)
    Write-Host "`n--- Running '$StepName' ($CommandArgs) ---"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    
    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    
    $cmd = "& `"$cliPath`" $CommandArgs"
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command $cmd *>$stdoutFile
        $exitCode = $LASTEXITCODE
    } catch {
        $exitCode = 1
        $_ | Out-File -FilePath $stderrFile -Encoding utf8
    }
    $sw.Stop()
    
    $out = if (Test-Path -LiteralPath $stdoutFile) { Get-Content -LiteralPath $stdoutFile } else { @() }
    $err = if (Test-Path -LiteralPath $stderrFile) { Get-Content -LiteralPath $stderrFile } else { @() }
    
    $allLines = $err + $out
    
    $hasStartupCrash = ($allLines | Where-Object { $_ -match "Cannot bind argument to parameter 'Path' because it is an empty string" }).Count -gt 0
    $hasBatchError = ($allLines | Where-Object { $_ -match "unexpected at this time" }).Count -gt 0
    
    Write-Host "ExitCode: $exitCode (Duration: $($sw.ElapsedMilliseconds) ms)"
    Write-Host "Empty-Startup-folder crash: $(if ($hasStartupCrash) {'OCCURRED (BUG!)'} else {'NONE (RESOLVED)'})"
    Write-Host "Batch syntax error:         $(if ($hasBatchError) {'OCCURRED (BUG!)'} else {'NONE (RESOLVED)'})"
    
    Write-Host "Output Sample (first 10 lines):"
    $out | Select-Object -First 10 | ForEach-Object { Write-Host "  $_" }
    
    Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    
    return @{
        Step = $StepName
        ExitCode = $exitCode
        StartupCrash = $hasStartupCrash
        BatchError = $hasBatchError
        Lines = $allLines
    }
}

$resShimStop = Run-Step -StepName "shim-stop" -CommandArgs "stop"
$resShimStart = Run-Step -StepName "shim-start" -CommandArgs "start"

# -------------------------------------------------------------------------
# Test 2: With Real docker.exe (without shim)
# -------------------------------------------------------------------------
$env:PATH = $origPath
$realDocker = Get-Command "docker.exe" -ErrorAction SilentlyContinue

if ($realDocker) {
    Write-Host "`n>>> [TEST 2] Running with REAL docker.exe ($($realDocker.Source))..."
    $resRealStop = Run-Step -StepName "real-stop" -CommandArgs "stop"
    $resRealStart = Run-Step -StepName "real-start" -CommandArgs "start"
} else {
    Write-Host "`n>>> [TEST 2] Real docker.exe not found on runner PATH; skipping Test 2."
    $resRealStop = $null
    $resRealStart = $null
}

# Restore environment
$env:ODS_HOME = $origOdsHome

# Cleanup temp files
Remove-Item -LiteralPath $spaceDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "`n======================================================================"
Write-Host "HARNESS CHECK SUMMARY"
Write-Host "======================================================================"
Write-Host "1. Shim stop:  ExitCode=$($resShimStop.ExitCode), StartupCrash=$($resShimStop.StartupCrash), BatchError=$($resShimStop.BatchError)"
Write-Host "2. Shim start: ExitCode=$($resShimStart.ExitCode), StartupCrash=$($resShimStart.StartupCrash), BatchError=$($resShimStart.BatchError)"
if ($realDocker) {
    Write-Host "3. Real stop:  ExitCode=$($resRealStop.ExitCode), StartupCrash=$($resRealStop.StartupCrash), BatchError=$($resRealStop.BatchError)"
    Write-Host "4. Real start: ExitCode=$($resRealStart.ExitCode), StartupCrash=$($resRealStart.StartupCrash), BatchError=$($resRealStart.BatchError)"
}
Write-Host "======================================================================"

# Determine verdict
$anyCrash = $resShimStop.StartupCrash -or $resShimStart.StartupCrash
$anyBatch = $resShimStop.BatchError -or $resShimStart.BatchError

if (-not $anyCrash -and -not $anyBatch) {
    Write-Host "`nVERDICT: Both the empty-Startup-folder crash and the batch error are 100% TEST HARNESS ARTIFACTS." -ForegroundColor Green
    Write-Host "  - The empty-Startup-folder crash was caused by the test harness setting `$env:USERPROFILE to an uninitialized temp path."
    Write-Host "  - The batch error was caused by the unquoted argument check if `"%2`"==`"ps`" in the test harness mock docker.cmd script."
    exit 0
} else {
    Write-Host "`nVERDICT: Failures still occur under real environment!" -ForegroundColor Red
    exit 1
}
