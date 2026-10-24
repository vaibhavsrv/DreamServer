# ============================================================================
# test-windows-path-classes.ps1
# Evaluates real CLI commands and Windows Supervisor under 4 path classes:
# 1. Space ("User With Space")
# 2. Apostrophe ("O'Brien")
# 3. Non-ASCII ("José")
# 4. Long Path (> 260 characters)
# ============================================================================

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$cliPath = Join-Path $repoRoot "installers\windows\ods.ps1"
$shortcutLib = Join-Path $repoRoot "installers\windows\lib\app-shortcut.ps1"
$supervisorPath = Join-Path $repoRoot "experimental\windows-supervisor\supervisor.py"

$currentPs = (Get-Process -Id $PID).Path

Write-Host "======================================================================"
Write-Host "WINDOWS REACHABLE PATH CLASS AUDIT"
Write-Host "Platform: $([Environment]::OSVersion.VersionString) ($([IntPtr]::Size * 8)-bit)"
Write-Host "PowerShell Host: $currentPs ($($PSVersionTable.PSVersion))"
Write-Host "CLI: $cliPath"
Write-Host "Supervisor: $supervisorPath"
Write-Host "======================================================================"

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) "ods_path_audit_$(Get-Random)"
[System.IO.Directory]::CreateDirectory($testRoot) | Out-Null

# Setup mock Docker in PATH
$mockBin = Join-Path $testRoot "mock_bin"
[System.IO.Directory]::CreateDirectory($mockBin) | Out-Null
$dockerCmd = Join-Path $mockBin "docker.cmd"
@"
@echo off
if "%1"=="info" exit /b 0
if "%1"=="compose" (
    if "%2"=="ps" (
        echo NAME      IMAGE     COMMAND   SERVICE   CREATED   STATUS    PORTS
        exit /b 0
    )
    if "%2"=="up" exit /b 0
    if "%2"=="down" exit /b 0
    exit /b 0
)
exit /b 0
"@ | Set-Content -LiteralPath $dockerCmd -Encoding ASCII

$env:PATH = "$mockBin;$env:PATH"

$classes = @(
    @{ Name = "Space";       Folder = "John Smith" },
    @{ Name = "Apostrophe";  Folder = "O'Brien" },
    @{ Name = "Non-ASCII";   Folder = ("Jos" + [char]0x00E9) },
    @{ Name = "Long Path";   Folder = ("sub_" * 35) + "long_dir" }
)

function Run-CommandWithCapture {
    param(
        [string]$ClassName,
        [string]$CommandName,
        [scriptblock]$Action
    )
    Write-Host "`n----------------------------------------------------------------------"
    Write-Host "TESTING: [$ClassName] -> $CommandName"
    Write-Host "----------------------------------------------------------------------"
    
    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $exitCode = 0
    $firstFailure = $null
    
    try {
        & $Action *>$stdoutFile
        $exitCode = $LASTEXITCODE
    } catch {
        $exitCode = 1
        $_ | Out-File -FilePath $stderrFile -Encoding utf8
    }
    $sw.Stop()
    
    $outLines = @()
    if (Test-Path -LiteralPath $stdoutFile) {
        $outLines = Get-Content -LiteralPath $stdoutFile
    }
    $errLines = @()
    if (Test-Path -LiteralPath $stderrFile) {
        $errLines = Get-Content -LiteralPath $stderrFile
    }
    
    # Identify first failure / error
    foreach ($line in ($errLines + $outLines)) {
        if ($line -match "error|fail|exception|fatal|cannot|invalid|not found|path too long" -and $line -notmatch "0 error" -and -not $firstFailure) {
            $firstFailure = $line.Trim()
        }
    }
    
    Write-Host "RESULT: [$ClassName] [$CommandName] ExitCode=$exitCode (${sw.ElapsedMilliseconds} ms)"
    if ($firstFailure) {
        Write-Host "FIRST_FAILURE: $firstFailure" -ForegroundColor Red
    } else {
        Write-Host "FIRST_FAILURE: NONE (SUCCESS)" -ForegroundColor Green
    }
    Write-Host "OUTPUT_SAMPLE (first 10 lines):"
    $outLines | Select-Object -First 10 | ForEach-Object { Write-Host "  $_" }
    if ($errLines) {
        Write-Host "STDERR_SAMPLE:"
        $errLines | Select-Object -First 5 | ForEach-Object { Write-Host "  STDERR: $_" -ForegroundColor Yellow }
    }
    
    Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    
    return @{
        Class = $ClassName
        Command = $CommandName
        ExitCode = $exitCode
        FirstFailure = $firstFailure
        DurationMs = $sw.ElapsedMilliseconds
    }
}

$summaryResults = @()

foreach ($cls in $classes) {
    $className = $cls.Name
    $folderName = $cls.Folder
    
    Write-Host "`n======================================================================"
    Write-Host "SETTING UP PATH CLASS: $className ($folderName)"
    Write-Host "======================================================================"
    
    $basePath = Join-Path $testRoot $folderName
    $userProf = Join-Path $basePath "UserProfile"
    $localApp = Join-Path $userProf "AppData\Local"
    $odsHome = Join-Path $basePath "ODS"
    
    $dirOk = $true
    try {
        [System.IO.Directory]::CreateDirectory($odsHome) | Out-Null
        [System.IO.Directory]::CreateDirectory($localApp) | Out-Null
    } catch {
        $dirOk = $false
        Write-Host "FAILED TO CREATE DIRECTORIES FOR ${className}: $($_.Exception.Message)" -ForegroundColor Red
        $summaryResults += @{
            Class = $className
            Command = "DirectoryCreation"
            ExitCode = 1
            FirstFailure = $_.Exception.Message
            DurationMs = 0
        }
        continue
    }
    
    # Minimal .env
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
    
    # Save original env
    $origUserProf = $env:USERPROFILE
    $origLocalApp = $env:LOCALAPPDATA
    $origOdsHome = $env:ODS_HOME
    
    $env:USERPROFILE = $userProf
    $env:LOCALAPPDATA = $localApp
    $env:ODS_HOME = $odsHome
    
    # Command 1: status
    $res = Run-CommandWithCapture -ClassName $className -CommandName "status" -Action {
        & $currentPs -NoProfile -ExecutionPolicy Bypass -File $cliPath status
    }
    $summaryResults += $res
    
    # Command 2: stop
    $res = Run-CommandWithCapture -ClassName $className -CommandName "stop" -Action {
        & $currentPs -NoProfile -ExecutionPolicy Bypass -File $cliPath stop
    }
    $summaryResults += $res
    
    # Command 3: start
    $res = Run-CommandWithCapture -ClassName $className -CommandName "start" -Action {
        & $currentPs -NoProfile -ExecutionPolicy Bypass -File $cliPath start
    }
    $summaryResults += $res
    
    # Command 4: config show
    $res = Run-CommandWithCapture -ClassName $className -CommandName "config" -Action {
        & $currentPs -NoProfile -ExecutionPolicy Bypass -File $cliPath config show
    }
    $summaryResults += $res
    
    # Command 5: shortcut creation
    $res = Run-CommandWithCapture -ClassName $className -CommandName "shortcut" -Action {
        $desk = Join-Path $userProf "Desktop"
        $progs = Join-Path $localApp "Programs"
        [System.IO.Directory]::CreateDirectory($desk) | Out-Null
        [System.IO.Directory]::CreateDirectory($progs) | Out-Null
        . $shortcutLib
        Publish-ODSPortalShortcut -PortalUrl "http://localhost:3001/pixel" -DesktopFolder $desk -ProgramsFolder $progs
    }
    $summaryResults += $res
    
    # Command 6: host-agent start
    $res = Run-CommandWithCapture -ClassName $className -CommandName "host-agent-start" -Action {
        & $currentPs -NoProfile -ExecutionPolicy Bypass -File $cliPath host-agent start
    }
    $summaryResults += $res
    
    # Command 7: Supervisor from PR #7953
    $res = Run-CommandWithCapture -ClassName $className -CommandName "supervisor" -Action {
        $supData = Join-Path $basePath "sup_data"
        [System.IO.Directory]::CreateDirectory($supData) | Out-Null
        python $supervisorPath --stub-children --stop-after-ready --data-dir $supData
    }
    $summaryResults += $res
    
    # Restore original env
    $env:USERPROFILE = $origUserProf
    $env:LOCALAPPDATA = $origLocalApp
    $env:ODS_HOME = $origOdsHome
}

Write-Host "`n======================================================================"
Write-Host "PATH CLASS AUDIT SUMMARY MATRIX"
Write-Host "======================================================================"
Write-Host ("{0,-15} | {1,-18} | {2,-8} | {3}" -f "Class", "Command", "ExitCode", "First Failure")
Write-Host ("-" * 75)
foreach ($r in $summaryResults) {
    $ff = if ($r.FirstFailure) { $r.FirstFailure } else { "PASS" }
    Write-Host ("{0,-15} | {1,-18} | {2,-8} | {3}" -f $r.Class, $r.Command, $r.ExitCode, $ff)
}
Write-Host ("-" * 75)

# Clean up test root
try { Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue } catch {}
