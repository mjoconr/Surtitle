<#
.SYNOPSIS
    Surtitle installer for Windows.

.DESCRIPTION
    One command that leaves a working installation:

        .\scripts\install.ps1                # install everything, including local voice
        .\scripts\install.ps1 -Update        # bring an existing install up to date
        .\scripts\install.ps1 -NoVoice       # hosted voice only (smaller, faster)
        .\scripts\install.ps1 -Check         # verify without changing anything

    What it does, and why in this order:

      1. Finds (or installs) uv, which supplies Python too. Nothing is installed
         system-wide, nothing is written to the registry or to PATH beyond the
         user's own profile, and no administrator rights are needed.
      2. Creates a virtual environment and installs Surtitle into it.
      3. Optionally installs the `voice-local` extra (sherpa-onnx) and downloads
         the speech models into the *app data* directory.

    Step 3's split matters for updates. The code lives in this folder; the models
    live under %LOCALAPPDATA%\Surtitle\models. Updating or replacing the code
    therefore never re-downloads ~86 MB of models, and deleting the folder never
    destroys them.

    The script is idempotent: running it twice is a fast no-op. -Update
    additionally refreshes the environment.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Yes

.NOTES
    If PowerShell refuses to run this script, use the line above: the module
    scope is not needed, but the execution policy is.
#>
[CmdletBinding()]
param(
    # Do not install the local speech engines or download models.
    [switch] $NoVoice,
    # Install the local engine but skip the model download.
    [switch] $NoModels,
    # Refresh an existing installation and re-verify it.
    [switch] $Update,
    # Report what is installed and missing; change nothing.
    [switch] $Check,
    # Answer yes to the model-download confirmation.
    [switch] $Yes
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

function Write-Step([string] $Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}
function Write-Info([string] $Message) { Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Warn([string] $Message) { Write-Host "warning: $Message" -ForegroundColor Yellow }
function Fail([string] $Message) {
    Write-Host ''
    Write-Host "error: $Message" -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
$dataDir = if ($env:SURTITLE_HOME) {
    $env:SURTITLE_HOME
} else {
    Join-Path $env:LOCALAPPDATA 'Surtitle'
}
$modelsDir = if ($env:SURTITLE_MODELS_DIR) {
    $env:SURTITLE_MODELS_DIR
} else {
    Join-Path $dataDir 'models'
}

Write-Host ''
Write-Host 'Surtitle installer - Windows' -ForegroundColor White
Write-Info "project: $ProjectDir"
Write-Info "data:    $dataDir"

# --------------------------------------------------------------------------- #
# uv
# --------------------------------------------------------------------------- #
function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $fallback = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $fallback) { return $fallback }
    return $null
}

$uv = Find-Uv
if (-not $uv) {
    if ($Check) { Fail 'uv is not installed. Run without -Check to install it.' }
    Write-Step 'Installing uv (Python toolchain, no admin rights needed)'
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Fail @"
Could not install uv automatically: $($_.Exception.Message)

Install it yourself, then re-run this script:

    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
"@
    }
    $uv = Find-Uv
    if (-not $uv) {
        Fail 'uv was installed but is not on PATH. Open a new PowerShell window and re-run.'
    }
}
Write-Info "uv:      $uv"

# --------------------------------------------------------------------------- #
# Virtual environment and dependencies
# --------------------------------------------------------------------------- #
$venv = Join-Path $ProjectDir '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'

if (-not $Check) {
    Write-Step 'Preparing the Python environment'
    if (-not (Test-Path $venvPy)) {
        & $uv venv --allow-existing $venv --quiet
        if ($LASTEXITCODE -ne 0) { Fail 'could not create the virtual environment' }
        Write-Info "created $venv"
    } else {
        Write-Info "$venv already exists"
    }

    $syncArgs = @('sync', '--python', $venvPy, '--quiet')
    if ($Update) { $syncArgs += '--refresh' }
    if (-not $NoVoice) { $syncArgs += @('--extra', 'voice-local') }

    & $uv @syncArgs
    if ($LASTEXITCODE -ne 0) {
        if (-not $NoVoice) {
            Write-Warn 'the local voice extra failed to install (no wheel for this platform?)'
            Write-Warn 'falling back to a hosted-voice-only install'
            & $uv sync --python $venvPy --quiet
            if ($LASTEXITCODE -ne 0) { Fail 'dependency installation failed' }
        } else {
            Fail 'dependency installation failed. Run "uv sync" to see the full output.'
        }
    }
    Write-Info 'dependencies installed'
}

# --------------------------------------------------------------------------- #
# Model download, via the application's own command
# --------------------------------------------------------------------------- #
function Invoke-App([string[]] $AppArguments) {
    $previous = $env:SURTITLE_HOME
    $env:SURTITLE_HOME = $dataDir
    try {
        if (Test-Path $venvPy) {
            & $venvPy -m surtitle @AppArguments
        } else {
            & $uv run --quiet --python $venvPy surtitle @AppArguments
        }
        return $LASTEXITCODE
    } finally {
        $env:SURTITLE_HOME = $previous
    }
}

if ($Check) {
    Write-Step 'Checking the installation'
    if (Test-Path $venvPy) {
        Invoke-App @('models', 'list') | Out-Null
    } else {
        Write-Warn "no virtual environment at $venv"
    }
    Write-Host ''
    Write-Host 'Re-run without -Check to install.' -ForegroundColor DarkGray
    exit 0
}

if ((-not $NoVoice) -and (-not $NoModels)) {
    Write-Step 'Local speech models'
    Write-Info "cache: $modelsDir (this survives updates and is removed only by you)"
    # The application prints sizes and asks before downloading ~86 MB. Answering
    # yes here is what makes the installer non-interactive.
    $modelArgs = @('models', 'download')
    if ($Yes -or -not $Host.Name.Contains('Console')) { $modelArgs += '--yes' }
    $code = Invoke-App $modelArgs
    if ($code -ne 0) {
        Write-Warn 'model download did not complete. Retry later with:'
        Write-Warn "  $ProjectDir\scripts\run.ps1 models download"
    }
}

# --------------------------------------------------------------------------- #
# Verify, then say what to do next
# --------------------------------------------------------------------------- #
Write-Step 'Verifying'
Invoke-App @('doctor', '--offline') | Out-Null

Write-Host ''
Write-Host 'Installation complete.' -ForegroundColor Green
@"

Start it with:

    $ProjectDir\scripts\run.ps1

Then, in the app:
  - Settings -> API keys: add a DeepSeek key (required) and a Deepgram key if you
    want hosted voice.
  - Settings -> Voice: set Speech-to-text and Text-to-speech to "local" to use the
    downloaded models - no key, no network.

Useful commands:

    $ProjectDir\scripts\run.ps1 models list      # what is installed
    $ProjectDir\scripts\run.ps1 models download  # fetch or repair models
    $ProjectDir\scripts\run.ps1 doctor           # live check of keys and engines
    $ProjectDir\scripts\install.ps1 -Update      # update this installation

Your data lives in: $dataDir
"@ | Write-Host
