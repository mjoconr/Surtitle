@echo off
REM Surtitle setup for Windows - double-click this file.
REM
REM This is the one-step entry point for a source checkout. It installs uv and
REM Python, the dependencies, optionally the offline speech engines and their
REM models, adds a Start Menu entry, and asks whether Surtitle should start when
REM you sign in.
REM
REM Arguments are forwarded to scripts\install.ps1, so the switches still work:
REM
REM     Setup.bat -NoVoice      hosted voice only (smaller, faster)
REM     Setup.bat -NoModels     install the engines but skip the ~86 MB download
REM     Setup.bat -Update       update an existing installation
REM     Setup.bat -Check        report what is installed; change nothing
REM
REM The launchers are untouched by this file: run.bat and run.ps1 still start the
REM app and forward any command (run.bat doctor, run.bat models list, ...).

setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"

REM A fresh Windows usually has an execution policy of Restricted, which refuses
REM install.ps1 outright - and a double-click gives the user nowhere to type the
REM bypass. Scope the relaxation to this one invocation rather than asking anyone
REM to change a machine-wide setting.
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\install.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo Setup did not finish successfully ^(exit %EXITCODE%^). The messages above say why.
    echo.
)

REM A double-clicked window closes the instant the script ends, taking the result
REM and any failure with it. Hold it open.
echo Press any key to close this window.
pause >nul
exit /b %EXITCODE%
