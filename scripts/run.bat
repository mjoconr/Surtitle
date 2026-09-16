@echo off
REM Surtitle launcher for Windows.
REM
REM Prefers the bundled runtime from a release archive, so no system Python and
REM no administrator rights are needed. Falls back to a source checkout built
REM with uv, then to an existing .venv.
REM
REM All arguments are forwarded, e.g.  run.bat doctor

setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM --- 1. bundled release runtime -------------------------------------------
REM A Windows archive installs into the bundled runtime rather than a virtual
REM environment, because a Windows venv records an absolute base path and so
REM cannot be moved once the archive is extracted.
set "BUNDLED=%SCRIPT_DIR%venv\Scripts\python.exe"
if not exist "%BUNDLED%" set "BUNDLED=%SCRIPT_DIR%python\python.exe"
if exist "%BUNDLED%" (
    "%BUNDLED%" -m surtitle %*
    set "EXITCODE=%ERRORLEVEL%"
    goto :done
)

REM --- 2. source checkout with uv -------------------------------------------
where uv >nul 2>&1
if %ERRORLEVEL%==0 (
    echo Syncing dependencies with uv...
    REM --inexact matters: a bare "uv sync" prunes anything the lock does not
    REM name, which silently deletes the optional voice-local extra. "uv run"
    REM then syncs again by default, so it has to be told not to as well.
    uv sync --inexact --quiet
    if errorlevel 1 (
        echo.
        echo error: uv sync failed. Run "uv sync" to see the full output.
        set "EXITCODE=1"
        goto :done
    )
    uv run --no-sync --quiet surtitle %*
    set "EXITCODE=%ERRORLEVEL%"
    goto :done
)

REM --- 3. existing development virtual environment --------------------------
if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    "%SCRIPT_DIR%.venv\Scripts\python.exe" -m surtitle %*
    set "EXITCODE=%ERRORLEVEL%"
    goto :done
)

REM --- nothing usable -------------------------------------------------------
echo.
echo Surtitle cannot start: no Python environment was found.
echo.
echo This looks like a source checkout with nothing installed yet.
echo.
echo Install uv ^(recommended - it fetches Python and dependencies for you^):
echo.
echo     powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo     run.bat
echo.
echo Or create a virtual environment yourself:
echo.
echo     python -m venv .venv
echo     .venv\Scripts\pip install -e .
echo     run.bat
echo.
set "EXITCODE=1"

:done
REM Keep the window open on a hard failure so a double-click shows the reason.
if not "%EXITCODE%"=="0" (
    if "%SURTITLE_NO_PAUSE%"=="" pause
)
endlocal & exit /b %EXITCODE%
