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

function Find-Uv {
    # uv's installer updates the user PATH but not the environment of the shell
    # that ran it, so a uv installed a moment ago is invisible to Get-Command in
    # the very window that installed it. Its documented location is checked too,
    # which is what makes "install uv, then run.ps1" work as written instead of
    # failing with advice to install the thing that was just installed.
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $fallback = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $fallback) { return $fallback }
    return $null
}

function Find-DevVenv {
    # The launcher's own directory in a release archive; its parent in a source
    # checkout, where .venv belongs and this file lives in scripts\.
    foreach ($candidate in @(
            (Join-Path $ScriptDir '.venv\Scripts\python.exe'),
            (Join-Path $ScriptDir '..\.venv\Scripts\python.exe')
        )) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
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

    # Where a development virtual environment would be, if there is one.
    #
    # Two layouts have to be covered. In a release archive this file sits at the
    # archive root, next to the bundled venv\ and python\. In a source checkout it
    # lives in scripts\, and the README tells you to create .venv at the checkout
    # root - so the directory to look in is the parent. Checking only the
    # launcher's own directory is why an existing .venv still reported that no
    # Python environment was found.
    $devVenv = Find-DevVenv

    # --- 2. source checkout with uv ---------------------------------------
    $uv = Find-Uv
    if ($uv) {
        # --inexact matters: a plain `uv sync` prunes anything the lock does not
        # name, which silently deletes the optional voice-local extra on every
        # run. `uv run` then syncs again by default, so it has to be told not to.
        #
        # Quiet on a warm checkout, loud on a cold one: a first run pulls the
        # whole dependency set, and several silent minutes are indistinguishable
        # from a hang.
        if ($devVenv) {
            Write-Host 'Syncing dependencies with uv...' -ForegroundColor DarkGray
            & $uv sync --inexact --quiet
        } else {
            Write-Host 'First run: fetching Python and dependencies.' -ForegroundColor DarkGray
            Write-Host 'This takes a few minutes, and only happens once.' -ForegroundColor DarkGray
            & $uv sync --inexact
        }
        if ($LASTEXITCODE -ne 0) { Fail 'uv sync failed. Run "uv sync" to see the full output.' }
        & $uv run --no-sync --quiet surtitle @Arguments
        exit $LASTEXITCODE
    }

    # --- 3. existing development virtual environment ----------------------
    if ($devVenv) {
        & $devVenv -m surtitle @Arguments
        exit $LASTEXITCODE
    }

    Fail @'
Surtitle cannot start: no Python environment was found.

This looks like a source checkout with nothing installed yet.

One command does everything - Python, dependencies, and the .venv:

    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

Or install uv yourself (it fetches Python and dependencies for you):

    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

uv unpacks into %USERPROFILE%\.local\bin and only joins the PATH of windows
opened afterwards. This launcher looks there too, so either open a new window and
run it again, or add it to this one:

    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

Or create a virtual environment yourself:

    python -m venv .venv
    .venv\Scripts\pip install -e .
    .\scripts\run.ps1
'@
}
finally {
    Pop-Location
}
