# Probe script to measure exact behavior of PowerShell cmdlets and .NET with square brackets in paths
# across PowerShell 7 (Core) and Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'

Write-Output "=== POWERSHELL RUNTIME INFO ==="
Write-Output "PSVersion: $($PSVersionTable.PSVersion)"
Write-Output "PSEdition: $($PSVersionTable.PSEdition)"
Write-Output "OS: $($env:OS)"

$scratch = Join-Path ([IO.Path]::GetTempPath()) ('ods-probe-' + [guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($scratch) | Out-Null

$results = [System.Collections.Generic.List[PSCustomObject]]::new()

function Record-Probe {
    param(
        [string]$Category,
        [string]$Scenario,
        [scriptblock]$Action
    )
    $status = "PASS"
    $detail = ""
    try {
        $detail = & $Action
    } catch {
        $status = "FAIL"
        $detail = $_.Exception.Message
    }
    Write-Output "[$status] $Category :: $Scenario -> $detail"
    $script:results.Add([PSCustomObject]@{
        PSVersion = $PSVersionTable.PSVersion.ToString()
        Category  = $Category
        Scenario  = $Scenario
        Status    = $status
        Detail    = "$detail"
    })
}

try {
    # Prepare directories: plain and bracketed
    $plainDir = Join-Path $scratch 'plain-dir'
    $bracketDir = Join-Path $scratch 'bracket-[test]'
    $bracketAbDir = Join-Path $scratch 'bracket-[a]b'
    $bracketOpenDir = Join-Path $scratch 'bracket-a['
    $bracketCloseDir = Join-Path $scratch 'bracket-]a'

    [System.IO.Directory]::CreateDirectory($plainDir) | Out-Null
    [System.IO.Directory]::CreateDirectory($bracketDir) | Out-Null
    [System.IO.Directory]::CreateDirectory($bracketAbDir) | Out-Null
    [System.IO.Directory]::CreateDirectory($bracketOpenDir) | Out-Null
    [System.IO.Directory]::CreateDirectory($bracketCloseDir) | Out-Null

    # Source fixture
    $srcFixture = Join-Path $scratch 'fixture.txt'
    [IO.File]::WriteAllText($srcFixture, 'fixture content')
    $fixtureUri = ([System.Uri]([IO.Path]::GetFullPath($srcFixture))).AbsoluteUri

    # 1. Test-Path
    Record-Probe "Test-Path" "Plain path without -LiteralPath" {
        $f = Join-Path $plainDir 'test.txt'; [IO.File]::WriteAllText($f, 'x')
        if (Test-Path $f) { "Found" } else { throw "Not found" }
    }
    Record-Probe "Test-Path" "Bracketed path without -LiteralPath" {
        $f = Join-Path $bracketDir 'test.txt'; [IO.File]::WriteAllText($f, 'x')
        if (Test-Path $f) { "Found" } else { throw "Wildcard evaluation failed to find file" }
    }
    Record-Probe "Test-Path" "Bracketed path with -LiteralPath" {
        $f = Join-Path $bracketDir 'test.txt'
        if (Test-Path -LiteralPath $f) { "Found literally" } else { throw "Not found literally" }
    }

    # 2. Get-Item
    Record-Probe "Get-Item" "Bracketed path without -LiteralPath" {
        $f = Join-Path $bracketDir 'test.txt'
        $item = Get-Item $f -ErrorAction Stop
        "Got: $($item.FullName)"
    }
    Record-Probe "Get-Item" "Bracketed path with -LiteralPath" {
        $f = Join-Path $bracketDir 'test.txt'
        $item = Get-Item -LiteralPath $f -ErrorAction Stop
        "Got literally: $($item.FullName)"
    }

    # 3. New-Item -Path vs [System.IO.Directory]::CreateDirectory
    Record-Probe "Directory Creation" "New-Item -ItemType Directory -Path with brackets" {
        $d = Join-Path $scratch 'new-item-[test]'
        New-Item -ItemType Directory -Path $d -Force -ErrorAction Stop | Out-Null
        "Created with New-Item"
    }
    Record-Probe "Directory Creation" "[System.IO.Directory]::CreateDirectory with brackets" {
        $d = Join-Path $scratch 'net-dir-[test]'
        [System.IO.Directory]::CreateDirectory($d) | Out-Null
        "Created with .NET"
    }

    # 4. curl.exe -o behavior on bracketed paths
    Record-Probe "curl.exe" "curl -o to bracketed path" {
        $dst = Join-Path $bracketDir 'curl-out.txt'
        & curl.exe -s -o $dst $fixtureUri
        if ($LASTEXITCODE -ne 0) { throw "curl exited with $LASTEXITCODE" }
        if (-not [IO.File]::Exists($dst)) { throw "Destination file not created" }
        "Downloaded: $([IO.File]::ReadAllText($dst))"
    }

    # 5. Invoke-WebRequest -OutFile behavior on bracketed paths
    Record-Probe "Invoke-WebRequest" "Invoke-WebRequest -OutFile to bracketed path" {
        $dst = Join-Path $bracketDir 'iwr-out.txt'
        Invoke-WebRequest -Uri $fixtureUri -OutFile $dst -ErrorAction Stop
        if (-not [IO.File]::Exists($dst)) { throw "Destination file not created" }
        "Downloaded with IWR: $([IO.File]::ReadAllText($dst))"
    }

    # 6. Move-Item -Path vs Move-Item -LiteralPath with -Destination
    Record-Probe "Move-Item" "Move-Item -Path with bracketed source" {
        $src = Join-Path $bracketDir 'move-src1.txt'; [IO.File]::WriteAllText($src, 'data1')
        $dst = Join-Path $bracketDir 'move-dst1.txt'
        Move-Item -Path $src -Destination $dst -Force -ErrorAction Stop
        if (-not [IO.File]::Exists($dst)) { throw "Destination not found" }
        "Moved via -Path"
    }
    Record-Probe "Move-Item" "Move-Item -LiteralPath with bracketed source to bracketed destination" {
        $src = Join-Path $bracketDir 'move-src2.txt'; [IO.File]::WriteAllText($src, 'data2')
        $dst = Join-Path $bracketDir 'move-dst2.txt'
        Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
        if (-not [IO.File]::Exists($dst)) { throw "Destination not found" }
        "Moved via -LiteralPath"
    }
    Record-Probe "Move-Item" "Move-Item -LiteralPath to existing bracketed destination (overwrite)" {
        $src = Join-Path $bracketDir 'move-src3.txt'; [IO.File]::WriteAllText($src, 'new data')
        $dst = Join-Path $bracketDir 'move-dst3.txt'; [IO.File]::WriteAllText($dst, 'old data')
        Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
        if ([IO.File]::ReadAllText($dst) -ne 'new data') { throw "Content not updated" }
        "Overwritten via -LiteralPath -Force"
    }
    Record-Probe "Move-Item" "Move-Item -LiteralPath with brackets in destination filename ([a]b.txt)" {
        $src = Join-Path $bracketDir 'move-src4.txt'; [IO.File]::WriteAllText($src, 'bracketed filename')
        $dst = Join-Path $bracketDir '[a]b.txt'
        Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
        if (-not [IO.File]::Exists($dst)) { throw "Destination not found" }
        "Moved to filename with brackets"
    }
    Record-Probe "Move-Item" "Move-Item -LiteralPath with unmatched open bracket in filename (a[.txt)" {
        $src = Join-Path $bracketDir 'move-src5.txt'; [IO.File]::WriteAllText($src, 'open bracket')
        $dst = Join-Path $bracketDir 'a[.txt'
        Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
        if (-not [IO.File]::Exists($dst)) { throw "Destination not found" }
        "Moved to filename with unmatched open bracket"
    }
    Record-Probe "Move-Item" "Move-Item -LiteralPath with unmatched close bracket in filename (]a.txt)" {
        $src = Join-Path $bracketDir 'move-src6.txt'; [IO.File]::WriteAllText($src, 'close bracket')
        $dst = Join-Path $bracketDir ']a.txt'
        Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
        if (-not [IO.File]::Exists($dst)) { throw "Destination not found" }
        "Moved to filename with unmatched close bracket"
    }

    # 7. Copy-Item -Path vs Copy-Item -LiteralPath with -Destination
    Record-Probe "Copy-Item" "Copy-Item -Path with bracketed source" {
        $src = Join-Path $bracketDir 'copy-src1.txt'; [IO.File]::WriteAllText($src, 'data1')
        $dst = Join-Path $bracketDir 'copy-dst1.txt'
        Copy-Item -Path $src -Destination $dst -Force -ErrorAction Stop
        if (-not [IO.File]::Exists($dst)) { throw "Destination not found" }
        "Copied via -Path"
    }
    Record-Probe "Copy-Item" "Copy-Item -LiteralPath with bracketed source and destination" {
        $src = Join-Path $bracketDir 'copy-src2.txt'; [IO.File]::WriteAllText($src, 'data2')
        $dst = Join-Path $bracketDir 'copy-dst2.txt'
        Copy-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
        if (-not [IO.File]::Exists($dst)) { throw "Destination not found" }
        "Copied via -LiteralPath"
    }

    # 8. Rename-Item -Path vs Rename-Item -LiteralPath
    Record-Probe "Rename-Item" "Rename-Item -Path with bracketed parent" {
        $f = Join-Path $bracketDir 'rename-src1.txt'; [IO.File]::WriteAllText($f, 'rename1')
        Rename-Item -Path $f -NewName 'rename-dst1.txt' -ErrorAction Stop
        $target = Join-Path $bracketDir 'rename-dst1.txt'
        if (-not [IO.File]::Exists($target)) { throw "Renamed file not found" }
        "Renamed via -Path"
    }
    Record-Probe "Rename-Item" "Rename-Item -LiteralPath with bracketed parent" {
        $f = Join-Path $bracketDir 'rename-src2.txt'; [IO.File]::WriteAllText($f, 'rename2')
        Rename-Item -LiteralPath $f -NewName 'rename-dst2.txt' -ErrorAction Stop
        $target = Join-Path $bracketDir 'rename-dst2.txt'
        if (-not [IO.File]::Exists($target)) { throw "Renamed file not found" }
        "Renamed via -LiteralPath"
    }
    Record-Probe "Rename-Item" "Rename-Item -LiteralPath to new name containing brackets ([a]b.txt)" {
        $f = Join-Path $bracketDir 'rename-src3.txt'; [IO.File]::WriteAllText($f, 'rename3')
        Rename-Item -LiteralPath $f -NewName '[a]b-renamed.txt' -ErrorAction Stop
        $target = Join-Path $bracketDir '[a]b-renamed.txt'
        if (-not [IO.File]::Exists($target)) { throw "Renamed file not found" }
        "Renamed to bracketed name"
    }

    # 9. [System.IO.File]::Move and Replace
    Record-Probe "[IO.File]::Move" ".NET File.Move to new bracketed file" {
        $src = Join-Path $bracketDir 'netmove-src.txt'; [IO.File]::WriteAllText($src, 'net data')
        $dst = Join-Path $bracketDir 'netmove-dst.txt'
        [IO.File]::Move($src, $dst)
        if (-not [IO.File]::Exists($dst)) { throw "Destination not found" }
        "Moved with [IO.File]::Move"
    }
    Record-Probe "[IO.File]::Replace" ".NET File.Replace on bracketed file" {
        $src = Join-Path $bracketDir 'netrep-src.txt'; [IO.File]::WriteAllText($src, 'new net data')
        $dst = Join-Path $bracketDir 'netrep-dst.txt'; [IO.File]::WriteAllText($dst, 'old net data')
        $bak = Join-Path $bracketDir 'netrep-bak.txt'
        [IO.File]::Replace($src, $dst, $bak)
        if ([IO.File]::ReadAllText($dst) -ne 'new net data') { throw "Destination not replaced" }
        "Replaced with [IO.File]::Replace"
    }

    # 10. Expand-Archive -LiteralPath
    Record-Probe "Expand-Archive" "Expand-Archive -LiteralPath into bracketed DestinationPath" {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        $zipSrc = Join-Path $scratch 'zip-src'
        [System.IO.Directory]::CreateDirectory($zipSrc) | Out-Null
        [IO.File]::WriteAllText((Join-Path $zipSrc 'inner.txt'), 'hello from zip')
        $testZip = Join-Path $bracketDir 'test-archive.zip'
        if (Test-Path -LiteralPath $testZip) { Remove-Item -LiteralPath $testZip -Force }
        [System.IO.Compression.ZipFile]::CreateFromDirectory($zipSrc, $testZip)
        $unpacked = Join-Path $bracketDir 'unpacked'
        Expand-Archive -LiteralPath $testZip -DestinationPath $unpacked -Force -ErrorAction Stop
        $inner = Join-Path $unpacked 'inner.txt'
        if (-not [IO.File]::Exists($inner)) { throw "Extracted file not found" }
        "Extracted: $([IO.File]::ReadAllText($inner))"
    }

    Write-Output "=== PROBE SUMMARY ==="
    $results | Format-Table -AutoSize
} finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}
