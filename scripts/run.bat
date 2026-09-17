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

REM --- no arguments means "start the app" ------------------------------------
REM Double-clicking this file is the documented first run, and that passes no
REM arguments at all. Forwarding an empty argument list to the CLI printed its
REM help and exited, so the window flashed and vanished without starting
REM anything. `run` is what a bare launch has always meant.
set "DEFAULT_COMMAND="
if "%~1"=="" set "DEFAULT_COMMAND=run"

REM --- 1. bundled release runtime -------------------------------------------
REM A Windows archive installs into the bundled runtime rather than a virtual
REM environment, because a Windows venv records an absolute base path and so
REM cannot be moved once the archive is extracted.
set "BUNDLED=%SCRIPT_DIR%venv\Scripts\python.exe"
if not exist "%BUNDLED%" set "BUNDLED=%SCRIPT_DIR%python\python.exe"
if exist "%BUNDLED%" (
    "%BUNDLED%" -m surtitle %DEFAULT_COMMAND% %*
    set "EXITCODE=%ERRORLEVEL%"
    goto :done
)

REM --- 2. source checkout with uv -------------------------------------------
REM uv's installer updates the *user* PATH but not the environment of the shell
REM that ran it, so a uv installed one command ago is missing from `where` in the
REM very window that installed it. Looking in its documented location as well is
REM what makes "install uv, then run.bat" work as written, rather than stopping
REM with advice to install the thing that was just installed.
set "UV="
for /f "delims=" %%U in ('where uv 2^>nul') do if not defined UV set "UV=%%U"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"

REM Where a development virtual environment would be, if there is one.
REM
REM Two layouts have to be covered. In a release archive this file sits at the
REM archive root, next to the bundled venv\ and python\. In a source checkout it
REM lives in scripts\, and the README tells you to create .venv at the checkout
REM root - so the directory to look in is the parent. Checking only the launcher's
REM own directory is why ".venv already exists" still reported that no Python
REM environment was found.
set "DEV_VENV=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%DEV_VENV%" if exist "%SCRIPT_DIR%..\.venv\Scripts\python.exe" set "DEV_VENV=%SCRIPT_DIR%..\.venv\Scripts\python.exe"

if defined UV (
    REM --inexact matters: a bare "uv sync" prunes anything the lock does not
    REM name, which silently deletes the optional voice-local extra. "uv run"
    REM then syncs again by default, so it has to be told not to as well.
    REM
    REM Quiet on a warm checkout, loud on a cold one. A first run pulls the whole
    REM dependency set - hundreds of megabytes - and silence for several minutes
    REM is indistinguishable from a hang, which is exactly what the user has just
    REM been through once already.
    if exist "%DEV_VENV%" (
        echo Syncing dependencies with uv...
        "%UV%" sync --inexact --quiet
    ) else (
        echo First run: fetching Python and dependencies. This takes a few minutes,
        echo and only happens once.
        "%UV%" sync --inexact
    )
    if errorlevel 1 (
        echo.
        echo error: uv sync failed. Run "uv sync" to see the full output.
        set "EXITCODE=1"
        goto :done
    )
    "%UV%" run --no-sync --quiet surtitle %DEFAULT_COMMAND% %*
    set "EXITCODE=%ERRORLEVEL%"
    goto :done
)

REM --- 3. existing development virtual environment --------------------------
if exist "%DEV_VENV%" (
    "%DEV_VENV%" -m surtitle %DEFAULT_COMMAND% %*
    set "EXITCODE=%ERRORLEVEL%"
    goto :done
)

REM --- nothing usable -------------------------------------------------------
echo.
echo Surtitle cannot start: no Python environment was found.
echo.
echo This looks like a source checkout with nothing installed yet.
echo.
echo One command installs everything - Python, dependencies, and the .venv:
echo.
echo     powershell -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install.ps1"
echo.
echo Or install uv yourself ^(it fetches Python and dependencies^):
echo.
echo     powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo.
echo uv unpacks into %%USERPROFILE%%\.local\bin and only joins the PATH of
echo windows opened afterwards. This launcher looks there too, so either open a
echo new window and run this again, or set the PATH in this one:
echo.
echo     set "PATH=%%USERPROFILE%%\.local\bin;%%PATH%%"
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
