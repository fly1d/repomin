[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $Python
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Python = (Resolve-Path -LiteralPath $Python).Path
$DemoRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("repomin-windows-quickstart-" + [System.Guid]::NewGuid().ToString("N"))
$Case = Join-Path $DemoRoot "case"
$Reduced = Join-Path $DemoRoot "reduced"
$DoctorOutput = Join-Path $DemoRoot "doctor-output"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,
        [Parameter(Mandatory = $true)]
        [string] $Content
    )

    [System.IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
    $Bytes = [System.IO.File]::ReadAllBytes($Path)
    if (
        $Bytes.Length -ge 3 -and
        $Bytes[0] -eq 0xEF -and
        $Bytes[1] -eq 0xBB -and
        $Bytes[2] -eq 0xBF
    ) {
        throw "Fixture file unexpectedly contains a UTF-8 BOM: $Path"
    }
}

try {
    New-Item -ItemType Directory -Path $Case | Out-Null

    $Reproducer = @(
        'from pathlib import Path'
        'import sys'
        ''
        'text = Path("input.txt").read_text(encoding="utf-8")'
        'if "keep-me" not in text:'
        '    print("DIFFERENT_FAILURE", file=sys.stderr)'
        '    raise SystemExit(2)'
        ''
        'print("ORIGINAL_FAILURE", file=sys.stderr)'
        'raise SystemExit(1)'
        ''
    ) -join "`n"
    Write-Utf8NoBom (Join-Path $Case "reproduce.py") $Reproducer
    Write-Utf8NoBom (Join-Path $Case "input.txt") "keep-me`nremove-me`n"
    Write-Utf8NoBom (Join-Path $Case "unused.txt") "unrelated file`n"

    $SourceFiles = @(Get-ChildItem -LiteralPath $Case -File).Count
    if ($SourceFiles -ne 3) {
        throw "Expected 3 source files, found $SourceFiles"
    }

    Push-Location $Case
    try {
        $BaselineLines = @(& $Python reproduce.py 2>&1)
        $BaselineExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    $BaselineText = $BaselineLines -join "`n"
    if ($BaselineExit -ne 1 -or $BaselineText -notmatch "ORIGINAL_FAILURE") {
        throw "Expected baseline exit 1 with ORIGINAL_FAILURE; exit was $BaselineExit"
    }

    # cmd.exe needs the executable quoted when a temporary path contains spaces.
    $Oracle = '"{0}" reproduce.py' -f $Python

    & $Python -m repomin doctor $Case `
        --command $Oracle `
        --match "ORIGINAL_FAILURE" `
        --adapter none `
        --source-reducer none `
        --output $DoctorOutput
    if ($LASTEXITCODE -ne 0) {
        throw "Doctor preflight failed"
    }

    & $Python -m repomin $Case `
        --command $Oracle `
        --match "ORIGINAL_FAILURE" `
        --adapter none `
        --source-reducer none `
        --text-file input.txt `
        --output $Reduced
    if ($LASTEXITCODE -ne 0) {
        throw "Reduction failed"
    }

    $ReducedFiles = @(Get-ChildItem -LiteralPath $Reduced -File -Recurse)
    if ($ReducedFiles.Count -ne 2) {
        throw "Expected 2 reduced files, found $($ReducedFiles.Count)"
    }
    $ReducedNames = @($ReducedFiles.Name | Sort-Object)
    if (($ReducedNames -join ",") -ne "input.txt,reproduce.py") {
        throw "Unexpected reduced files: $($ReducedNames -join ', ')"
    }
    $ReducedInput = [System.IO.File]::ReadAllText(
        (Join-Path $Reduced "input.txt"),
        $Utf8NoBom
    )
    if ($ReducedInput -ne "keep-me`n") {
        throw "Expected reduced input.txt to contain only keep-me"
    }

    $Report = Join-Path ($Reduced + ".repomin") "report.json"
    $ValidationLines = @(
        & $Python -m repomin report validate $Report `
            --payload $Reduced `
            --json
    )
    $ValidationExit = $LASTEXITCODE
    if ($ValidationExit -ne 0) {
        throw "Report validation failed"
    }
    $Validation = ($ValidationLines -join "`n") | ConvertFrom-Json
    if (
        -not $Validation.valid -or
        -not $Validation.payload_fingerprint_verified -or
        $Validation.payload_fingerprint_mode -ne "exact" -or
        $Validation.source_files -ne 3 -or
        $Validation.output_files -ne 2
    ) {
        throw "Report validation did not prove an exact 3 -> 2 payload"
    }

    $ReplayLines = @(
        & $Python -m repomin report replay $Report `
            --payload $Reduced `
            --runs 2 `
            --yes 2>&1
    )
    $ReplayExit = $LASTEXITCODE
    $ReplayText = $ReplayLines -join "`n"
    if ($ReplayExit -ne 0 -or $ReplayText -notmatch "Fresh runs: 2/2 passed") {
        throw "Expected report replay to pass 2/2 fresh runs"
    }

    Write-Output (
        "Windows quick start smoke passed: " +
        "3 -> 2 files, exact payload fingerprint, replay 2/2."
    )
}
finally {
    if (Test-Path -LiteralPath $DemoRoot) {
        Remove-Item -LiteralPath $DemoRoot -Recurse -Force
    }
}
