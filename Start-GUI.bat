@echo off
setlocal
title LLM Orchestra — Launcher

set "AGENT_DIR=H:\AI\Agents\SWE"
set "PYTHON=%AGENT_DIR%\.venv\Scripts\python.exe"
set "SERVER=%AGENT_DIR%\gui_server.py"
set "URL=http://localhost:5000"

echo ==================================================
echo   LLM Orchestra — GUI Launcher
echo ==================================================
echo.

REM 1. Verify Python exists
if not exist "%PYTHON%" (
    echo [ERROR] Python not found at: %PYTHON%
    echo         Did you create the venv? Run:
    echo         python -m venv .venv
    pause
    exit /b 1
)

REM 2. Verify gui_server.py exists
if not exist "%SERVER%" (
    echo [ERROR] gui_server.py not found at: %SERVER%
    pause
    exit /b 1
)

REM 3. Check if port 5000 is already in use
netstat -ano | findstr /R /C:":5000 .*LISTENING" >nul
if %errorlevel% equ 0 (
    echo [INFO] Server already running on port 5000.
    echo        Opening browser...
    start "" "%URL%"
    timeout /t 2 /nobreak >nul
    exit /b 0
)

REM 4. Check LM Studio is reachable
echo Checking LM Studio at http://localhost:1234...
powershell -NoProfile -Command ^
  "try { Invoke-WebRequest -Uri 'http://localhost:1234/v1/models' -TimeoutSec 3 -UseBasicParsing | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel% neq 0 (
    echo.
    echo [WARNING] LM Studio does not appear to be running at http://localhost:1234
    echo           Start LM Studio, load the server, then re-run this script.
    echo.
    choice /M "Continue anyway"
    if errorlevel 2 exit /b 0
)
echo [OK] LM Studio reachable.
echo.

REM 5. Launch the server in a new window
echo [INFO] Starting GUI server...
start "LLM Orchestra Server" /D "%AGENT_DIR%" "%PYTHON%" "%SERVER%"

REM 6. Wait for server to respond on port 5000
echo [INFO] Waiting for server to bind to port 5000...
set /a retries=0
:waitLoop
timeout /t 1 /nobreak >nul
netstat -ano | findstr /R /C:":5000 .*LISTENING" >nul
if %errorlevel% equ 0 goto ready
set /a retries+=1
if %retries% lss 15 goto waitLoop

echo [ERROR] Server did not start within 15 seconds.
echo         Check the "LLM Orchestra Server" window for tracebacks.
pause
exit /b 1

:ready
echo [OK] Server ready.
echo.
echo Opening browser at %URL%
start "" "%URL%"

timeout /t 2 /nobreak >nul
exit /b 0