<#
.SYNOPSIS
    Surtitle launcher for Windows (PowerShell).

.DESCRIPTION
    Prefers the bundled runtime from a release archive, so no system Python and
    no administrator rights are needed. Falls back to a source checkout built
    with uv, then to an existing .venv.

.EXAMPLE
    .\run.ps1
    .\run.ps1 doctor
    .\run.ps1 run --port 9000 --no-browser

.NOTES
    If PowerShell blocks this script, run it with:
        powershell -ExecutionPolicy Bypass -File .\run.ps1
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ScriptDir

function Fail([string] $Message) {
    Write-Host ''
    Write-Host $Message -ForegroundColor Red
    Write-Host ''
    exit 1
}

try {
    # A "fat" release archive carries the speech models inside it. Pointing the app
    # at that directory keeps an extracted archive fully offline: the models are
    # found without a download and without touching the user's data directory.
    $bundledModels = Join-Path $ScriptDir 'models'
    if ((Test-Path $bundledModels) -and (-not $env:SURTITLE_MODELS_DIR)) {
        $env:SURTITLE_MODELS_DIR = $bundledModels
    }

    # --- 1. bundled release runtime ---------------------------------------
    # A Windows archive installs into the bundled runtime rather than a virtual
    # environment, because a Windows venv records an absolute base path and so
    # cannot be moved once the archive is extracted. A POSIX archive ships venv/.
    $bundled = Join-Path $ScriptDir 'venv\Scripts\python.exe'
    if (-not (Test-Path $bundled)) {
        $bundled = Join-Path $ScriptDir 'python\python.exe'
    }
    if (Test-Path $bundled) {
        & $bundled -m surtitle @Arguments
        exit $LASTEXITCODE
    }

    # --- 2. source checkout with uv ---------------------------------------
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Write-Host 'Syncing dependencies with uv...' -ForegroundColor DarkGray
        # --inexact matters: a plain `uv sync` prunes anything the lock does not
        # name, which silently deletes the optional voice-local extra on every
        # run. `uv run` then syncs again by default, so it has to be told not to.
        & $uv.Source sync --inexact --quiet
        if ($LASTEXITCODE -ne 0) { Fail 'uv sync failed. Run "uv sync" to see the full output.' }
        & $uv.Source run --no-sync --quiet surtitle @Arguments
        exit $LASTEXITCODE
    }

    # --- 3. existing development virtual environment ----------------------
    $venv = Join-Path $ScriptDir '.venv\Scripts\python.exe'
    if (Test-Path $venv) {
        & $venv -m surtitle @Arguments
        exit $LASTEXITCODE
    }

    Fail @'
Surtitle cannot start: no Python environment was found.

This looks like a source checkout with nothing installed yet.

Install uv (recommended - it fetches Python and dependencies for you):

    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    .\scripts\run.ps1

Or create a virtual environment yourself:

    python -m venv .venv
    .venv\Scripts\pip install -e .
    .\scripts\run.ps1
'@
}
finally {
    Pop-Location
}
