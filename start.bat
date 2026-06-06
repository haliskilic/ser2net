@echo off
REM ser2net launcher for Windows.
REM Requires Python 3.10+ on PATH (as "python" or "py").
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "PY=py -3"
    ) else (
        echo [ser2net] Python 3.10+ was not found on PATH ^(python or py^).
        echo            Install it from https://www.python.org/downloads/ and re-run.
        pause
        exit /b 1
    )
)

%PY% ser2net.py %*
if %errorlevel% neq 0 pause
endlocal
