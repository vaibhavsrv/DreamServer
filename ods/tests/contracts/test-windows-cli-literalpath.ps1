$ErrorActionPreference = 'Stop'

# Contract: ods.ps1 CLI install checks and PID cleanup must evaluate paths literally.
# Paths containing bracket characters (e.g., C:\Users\user[test]\ods or $env:ODS_HOME)
# must not fail Test-Path as wildcards, and PID cleanup must remove bracketed pid files.

$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$cliPath = Join-Path $root 'installers/windows/ods.ps1'

function Get-TestAst {
    param([string]$Path)
    $tokens = $null; $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw "Parse failed: $Path" }
    return $ast
}

$cliAst = Get-TestAst $cliPath

# Extract AST nodes
$testInstallNode = $cliAst.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-Install'
}, $true)
if (-not $testInstallNode) { throw 'Test-Install function not found in ods.ps1' }

$testInstallFilesNode = $cliAst.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-ODSInstallFiles'
}, $true)
if (-not $testInstallFilesNode) { throw 'Test-ODSInstallFiles function not found in ods.ps1' }

$stopServerNode = $cliAst.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-NativeInferenceServer'
}, $true)
if (-not $stopServerNode) { throw 'Stop-NativeInferenceServer function not found in ods.ps1' }

function Assert-Test([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    Write-Host "PASS $Message"
}

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("ods-cli-literalpath-" + [guid]::NewGuid().ToString('N'))
$InstallDir = Join-Path $fixtureRoot "app[bracket]\ods"
$null = New-Item -ItemType Directory -Path $InstallDir -Force

try {
    $baseCompose = Join-Path $InstallDir "docker-compose.base.yml"
    [IO.File]::WriteAllText($baseCompose, "services: {}")

    # 1. Baseline defect validation: bracketed path fails non-literal Test-Path
    Assert-Test (Test-Path -LiteralPath $InstallDir) "InstallDir with brackets exists literally"
    Assert-Test (-not (Test-Path $InstallDir)) "InstallDir with brackets fails non-literal Test-Path (wildcard defect baseline)"

    # 2. Verify AST of Test-Install uses -LiteralPath
    $testInstallText = $testInstallNode.Extent.Text
    Assert-Test ($testInstallText -match 'Test-Path\s+-LiteralPath\s+\$InstallDir') "Test-Install uses -LiteralPath for InstallDir"
    Assert-Test ($testInstallText -match 'Test-Path\s+-LiteralPath\s+\$baseCompose') "Test-Install uses -LiteralPath for baseCompose"

    # 3. Verify AST of Test-ODSInstallFiles uses -LiteralPath
    $testFilesText = $testInstallFilesNode.Extent.Text
    Assert-Test ($testFilesText -match 'Test-Path\s+-LiteralPath\s+\$InstallDir') "Test-ODSInstallFiles uses -LiteralPath for InstallDir"
    Assert-Test ($testFilesText -match 'Test-Path\s+-LiteralPath\s+\$baseCompose') "Test-ODSInstallFiles uses -LiteralPath for baseCompose"

    # 4. Verify AST of Stop-NativeInferenceServer uses -LiteralPath
    $stopServerText = $stopServerNode.Extent.Text
    Assert-Test ($stopServerText -match 'Test-Path\s+-LiteralPath\s+\$script:INFERENCE_PID_FILE') "Stop-NativeInferenceServer tests PID file with -LiteralPath"
    Assert-Test ($stopServerText -match 'Remove-Item\s+-LiteralPath\s+\$script:INFERENCE_PID_FILE') "Stop-NativeInferenceServer removes PID file with -LiteralPath"

    # 5. Functional PID file removal test on bracketed directory
    $dataDir = Join-Path $InstallDir "data"
    $null = New-Item -ItemType Directory -Path $dataDir -Force
    $pidFile = Join-Path $dataDir "llama-server.pid"
    [IO.File]::WriteAllText($pidFile, "12345")

    Assert-Test (Test-Path -LiteralPath $pidFile) "Bracketed PID file created"
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Assert-Test (Test-Path -LiteralPath $pidFile) "Unpatched Remove-Item leaves stale PID file behind"

    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Assert-Test (-not (Test-Path -LiteralPath $pidFile)) "Patched Remove-Item removes PID file cleanly"

} finally {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "All Windows CLI literal path contract checks passed."
