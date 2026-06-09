@echo off
REM ser2net launcher for Windows.
REM Requires Python 3.10+ on PATH (as "python" or "py").
setlocal

cd /d "%~dp0"

REM Detect the interpreter. NOTE: do NOT test %errorlevel% inside an if(...)else(...)
REM block — it is expanded when the block is parsed, not when it runs, so the
REM "py" fallback never fired. Use "&& set" + "if not defined" instead.
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    echo [ser2net] Python 3.10+ was not found on PATH ^(python or py^).
    echo            Install it from https://www.python.org/downloads/ and re-run.
    pause
    exit /b 1
)

%PY% ser2net.py %*
if errorlevel 1 pause
endlocal
