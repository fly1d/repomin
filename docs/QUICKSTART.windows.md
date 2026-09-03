# ReproMin quick start for Windows PowerShell

This guide installs the current development release in an isolated virtual
environment, creates a three-file failing project, checks it, reduces it, and
validates the exported evidence. Installation downloads the release wheel;
the example itself does not access the network.

ReproMin requires Python 3.9 or newer. Run these commands in PowerShell. Every
ReproMin command uses the virtual environment's explicit Python path, so the
workflow does not depend on activation policy or the current `PATH`.

## 1. Install in an isolated environment

Create a unique scratch directory and virtual environment:

```powershell
$DemoRoot = Join-Path `
  ([System.IO.Path]::GetTempPath()) `
  ("repomin-quickstart-" + [System.Guid]::NewGuid().ToString("N"))
$Venv = Join-Path $DemoRoot ".venv"
New-Item -ItemType Directory -Path $DemoRoot | Out-Null
py -3 -m venv $Venv
$Python = Join-Path $Venv "Scripts\python.exe"

& $Python -m pip install --upgrade pip
$env:REPOMIN_VERSION = "0.1.0.dev9"
& $Python -m pip install "https://github.com/fly1d/repomin/releases/download/v${env:REPOMIN_VERSION}/repomin-${env:REPOMIN_VERSION}-py3-none-any.whl"
& $Python -m repomin --version
```

The final command should print `repomin 0.1.0.dev9`. The
[release page](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev9)
publishes SHA-256 digests for users who need to verify the downloaded wheel.

## 2. Create a small failing project

Create the fixture as UTF-8 without a byte-order mark. Using an explicit
encoding avoids the different defaults in Windows PowerShell 5.1 and newer
PowerShell releases.

```powershell
$Case = Join-Path $DemoRoot "case"
$Reduced = Join-Path $DemoRoot "reduced"
$DoctorOutput = Join-Path $DemoRoot "doctor-output"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
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

[System.IO.File]::WriteAllText(
  (Join-Path $Case "reproduce.py"), $Reproducer, $Utf8NoBom)
[System.IO.File]::WriteAllText(
  (Join-Path $Case "input.txt"), "keep-me`nremove-me`n", $Utf8NoBom)
[System.IO.File]::WriteAllText(
  (Join-Path $Case "unused.txt"), "unrelated file`n", $Utf8NoBom)
```

Confirm the failure contract before reducing anything:

```powershell
Push-Location $Case
try {
  & $Python reproduce.py
  $BaselineExit = $LASTEXITCODE
}
finally {
  Pop-Location
}
if ($BaselineExit -ne 1) {
  throw "Expected baseline exit code 1, received $BaselineExit"
}
```

The command prints `ORIGINAL_FAILURE` and exits with status `1`. This marker is
the oracle: ReproMin accepts a candidate only while the command still fails and
its combined output still matches that text.

## 3. Preflight and reduce the project

Quote the explicit Python path in the recorded command because a Windows
temporary path can contain spaces. Doctor runs the failure oracle twice in
fresh copies without creating the configured output:

```powershell
$Oracle = '"{0}" reproduce.py' -f $Python

& $Python -m repomin doctor $Case `
  --command $Oracle `
  --match "ORIGINAL_FAILURE" `
  --adapter none `
  --source-reducer none `
  --output $DoctorOutput
if ($LASTEXITCODE -ne 0) { throw "Doctor preflight failed" }
```

A successful preflight reports `2/2 fresh runs reproduced the failure`. Run
the reduction with the same failure contract:

```powershell
& $Python -m repomin $Case `
  --command $Oracle `
  --match "ORIGINAL_FAILURE" `
  --adapter none `
  --source-reducer none `
  --text-file input.txt `
  --output $Reduced
if ($LASTEXITCODE -ne 0) { throw "Reduction failed" }
```

Check that the payload was reduced from three files to two and that the
required line remains:

```powershell
$SourceFiles = @(Get-ChildItem -LiteralPath $Case -File)
$ReducedFiles = @(Get-ChildItem -LiteralPath $Reduced -File -Recurse)
if ($SourceFiles.Count -ne 3 -or $ReducedFiles.Count -ne 2) {
  throw "Expected a 3 -> 2 file reduction"
}
$ReducedNames = @($ReducedFiles.Name | Sort-Object)
if (($ReducedNames -join ",") -ne "input.txt,reproduce.py") {
  throw "Unexpected reduced files: $($ReducedNames -join ', ')"
}
$ReducedInput = [System.IO.File]::ReadAllText(
  (Join-Path $Reduced "input.txt"), $Utf8NoBom)
if ($ReducedInput -ne "keep-me`n") {
  throw "Expected input.txt to contain only keep-me"
}
$ReducedNames
$ReducedInput
```

The files are `input.txt` and `reproduce.py`; `input.txt` contains only
`keep-me`.

## 4. Validate and replay the evidence

The payload and evidence sidecar are separate:

```text
reduced/                         reduced repository
reduced.repomin/report.json     machine-readable evidence
reduced.repomin/REPOMIN.md      human-readable receipt
```

Validate the report structure and exact payload fingerprint without rerunning
the failure command:

```powershell
$Report = Join-Path ($Reduced + ".repomin") "report.json"
$ValidationJson = & $Python -m repomin report validate $Report `
  --payload $Reduced `
  --json
if ($LASTEXITCODE -ne 0) { throw "Report validation failed" }

$Validation = $ValidationJson | ConvertFrom-Json
if (
  -not $Validation.valid -or
  -not $Validation.payload_fingerprint_verified -or
  $Validation.payload_fingerprint_mode -ne "exact"
) {
  throw "Expected a valid report with an exact payload fingerprint"
}
$ValidationJson
```

Before replay, review the command inside `report.json`. Replay explicitly
executes that recorded command in two fresh payload copies:

```powershell
$ReplayLines = @(
  & $Python -m repomin report replay $Report `
    --payload $Reduced `
    --runs 2 `
    --yes 2>&1
)
$ReplayExit = $LASTEXITCODE
$ReplayText = $ReplayLines -join "`n"
$ReplayLines
if ($ReplayExit -ne 0 -or $ReplayText -notmatch "Fresh runs: 2/2 passed") {
  throw "Expected report replay to pass 2/2 fresh runs"
}
```

A successful replay reports `Fresh runs: 2/2 passed`. The scratch directory
is available at `$DemoRoot` for inspection when the walkthrough finishes.

## Safety boundary

The default host backend runs the supplied command with your user account. It
is not a sandbox. Only use it with repositories and commands you trust. The
Docker backend reduces access when configured carefully, but it is not a
complete security boundary either. Do not publish credentials, private URLs,
proprietary source, customer data, raw logs, or environment values.

For macOS or Linux, follow the [Bash/Zsh quick start](QUICKSTART.md). For more
workflows, continue with the [examples guide](EXAMPLES.md).
