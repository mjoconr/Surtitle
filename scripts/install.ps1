<#
.SYNOPSIS
    Surtitle installer for Windows.

.DESCRIPTION
    Usually reached by double-clicking Setup.bat at the top of the checkout,
    which needs no terminal and no command. Run it directly if you prefer:

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
      4. Adds a Surtitle entry to the Start Menu for this user, pointing at the
         launcher. Per-user, so it needs no administrator rights and touches
         nothing outside the profile; -NoShortcut skips it.
      5. Asks whether Surtitle should start when this user signs in, and adds or
         removes a shortcut in the Startup folder accordingly. Ask once, never
         silently: -Startup and -NoStartup answer it for an automated run, and
         -Yes takes the default of not adding anything to sign-in.

    Step 3's split matters for updates. The code lives in this folder; the models
    live under %LOCALAPPDATA%\Surtitle\models. Updating or replacing the code
    therefore never re-downloads the speech models, and deleting the folder never
    destroys them.

    The script is idempotent: running it twice is a fast no-op. -Update
    additionally refreshes the environment.

.EXAMPLE
    .\Setup.bat

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Yes -Startup

.NOTES
    If PowerShell refuses to run this script, use Setup.bat, or the line above:
    the module scope is not needed, but the execution policy is.
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
    [switch] $Yes,
    # Do not add a Surtitle entry to the Start Menu.
    [switch] $NoShortcut,
    # Start Surtitle when this user signs in. Omitted: ask when interactive.
    [switch] $Startup,
    # Never start Surtitle at sign-in, and remove an entry if one exists.
    [switch] $NoStartup
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

function Test-CanPrompt {
    # A double-clicked Setup.bat has a console to answer on; a piped or CI run
    # does not, and blocking on Read-Host there would hang an unattended install.
    try { return -not [Console]::IsInputRedirected } catch { return $false }
}

function Ask-YesNo([string] $Question, [bool] $Default = $false) {
    $suffix = if ($Default) { '[Y/n]' } else { '[y/N]' }
    try { $answer = Read-Host "    $Question $suffix" } catch { return $Default }
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().ToLowerInvariant().StartsWith('y')
}

function New-SurtitleShortcut {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Target,
        [string[]] $TargetArguments,
        # 7 = minimized. A sign-in launch should not open a console over whatever
        # the user is doing; the icon and the Start Menu entry are how it is
        # reached afterwards.
        [int] $WindowStyle = 1
    )
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    if ($TargetArguments) { $shortcut.Arguments = ($TargetArguments -join ' ') }
    $shortcut.WorkingDirectory = $ProjectDir
    $shortcut.Description = 'Surtitle - voice-first agentic workbench'
    $iconFile = Join-Path $ProjectDir 'src\surtitle\web\surtitle.ico'
    if (Test-Path $iconFile) { $shortcut.IconLocation = "$iconFile,0" }
    if ($WindowStyle -ne 1) { $shortcut.WindowStyle = $WindowStyle }
    $shortcut.Save()
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
    # The application prints sizes and asks before downloading the speech models.
    # Answering yes here is what makes the installer non-interactive.
    $modelArgs = @('models', 'download')
    if ($Yes -or -not $Host.Name.Contains('Console')) { $modelArgs += '--yes' }
    $code = Invoke-App $modelArgs
    if ($code -ne 0) {
        Write-Warn 'model download did not complete. Retry later with:'
        Write-Warn "  $ProjectDir\scripts\run.ps1 models download"
    }
}

# --------------------------------------------------------------------------- #
# Start Menu and sign-in shortcuts
# --------------------------------------------------------------------------- #
# Per-user and outside Program Files, like everything else here: created without
# elevation and removed by deleting one .lnk.
$launcher = Join-Path $ProjectDir 'scripts\run.bat'

if (-not $NoShortcut) {
    Write-Step 'Start Menu shortcut'
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $shortcutPath = Join-Path $startMenu 'Surtitle.lnk'
    try {
        if (-not (Test-Path $startMenu)) {
            New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
        }
        # The application's own mark, so the entry is recognisable by sight
        # rather than being another generic script icon.
        New-SurtitleShortcut -Path $shortcutPath -Target $launcher
        Write-Info "created $shortcutPath"
        Write-Info 'pin it to the taskbar from there if you want it always to hand'
    } catch {
        # A Start Menu that cannot be written does not make the installation
        # broken, so this is a warning and never a failure.
        Write-Warn "could not create the Start Menu shortcut: $($_.Exception.Message)"
    }
}

Write-Step 'Start when you sign in'
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'Surtitle.lnk'
# -Startup and -NoStartup are the answers for an automated run. -Yes means "take
# the defaults" and adding something to sign-in is not a default worth taking
# silently, so it answers no.
$enableStartup = $false
if ($Startup) {
    $enableStartup = $true
} elseif ($NoStartup) {
    $enableStartup = $false
} elseif ((-not $Yes) -and (Test-CanPrompt)) {
    $enableStartup = Ask-YesNo 'Start Surtitle when you sign in?' $false
}
try {
    if ($enableStartup) {
        # --no-browser: a browser window opening itself at every sign-in is
        # intrusive. The app is ready behind the notification icon, and the
        # Start Menu entry (or the icon) opens the UI when it is wanted.
        New-SurtitleShortcut -Path $startupLink -Target $launcher `
            -TargetArguments @('run', '--no-browser') -WindowStyle 7
        Write-Info "Surtitle will start when you sign in: $startupLink"
        Write-Info 'delete that shortcut to turn it off'
    } elseif (Test-Path $startupLink) {
        Remove-Item $startupLink -Force
        Write-Info "removed the sign-in shortcut: $startupLink"
    } else {
        Write-Info 'Surtitle will not start automatically; start it from the Start Menu'
    }
} catch {
    # Sign-in is a convenience; failing to arrange it is not a broken install.
    Write-Warn "could not change the sign-in setting: $($_.Exception.Message)"
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

    $ProjectDir\scripts\run.bat

Double-click it, or use the Surtitle entry this installer added to the Start
Menu. Passing it a command still works exactly as before:

    run.bat doctor          check keys and engines
    run.bat models list     what is installed

To run this installer again without a terminal - to add the offline voice
engines later, for example - double-click Setup.bat in $ProjectDir. It accepts
the same switches:

    Setup.bat -NoVoice      hosted voice only
    Setup.bat -Update       update this installation
    Setup.bat -Startup      start Surtitle when you sign in (or -NoStartup)

Windows blocks PowerShell scripts by default (the policy is usually Restricted),
which is why the batch launchers are the ones that work untouched. From
PowerShell, the same launcher with the policy bypassed:

    powershell -ExecutionPolicy Bypass -File $ProjectDir\scripts\run.ps1

Then, in the app:
  - Settings -> API keys: add a DeepSeek key (required) and a Deepgram key if you
    want hosted voice.
  - Settings -> Voice: set Speech-to-text and Text-to-speech to "local" to use the
    downloaded models - no key, no network.

While it runs, a Surtitle icon sits in the taskbar notification area. Right-click
it for Status, Usage and Stop; the console window still works the same way.

Useful commands:

    $ProjectDir\scripts\run.bat models list      # what is installed
    $ProjectDir\scripts\run.bat models download  # fetch or repair models
    $ProjectDir\scripts\run.bat doctor           # live check of keys and engines
    powershell -ExecutionPolicy Bypass -File $ProjectDir\scripts\install.ps1 -Update

Your data lives in: $dataDir
"@ | Write-Host
