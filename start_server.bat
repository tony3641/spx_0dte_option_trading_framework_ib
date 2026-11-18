@echo off
rem Starts the SPX trade desk dashboard (http://localhost:8000) inside the project's .venv.
rem Activates the venv only if one is not already active in this shell.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [error] .venv not found in %CD%. Create it first:
    echo   py -3.14 -m venv .venv
    echo   .venv\Scripts\python -m pip install -e "C:\TWS API\source\pythonclient"
    echo   .venv\Scripts\python -m pip install -r requirements-dev.txt
    echo   .venv\Scripts\python -m pip install -e . --no-deps
    exit /b 1
)

if defined VIRTUAL_ENV (
    echo [info] Virtual environment already active: %VIRTUAL_ENV%
) else (
    echo [info] Activating .venv
    call ".venv\Scripts\activate.bat"
)

python -m spx_trade_desk.server %*
exit /b %ERRORLEVEL%
