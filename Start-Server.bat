@echo off
setlocal
title LLM Orchestra Backend

set "AGENT_DIR=H:\AI\Agents\SWE"
set "PYTHON=%AGENT_DIR%\.venv\Scripts\python.exe"
set "SERVER=%AGENT_DIR%\gui_server.py"

echo ==================================================
echo   LLM Orchestra Backend
echo   (Browser UI disabled - use the VS Code sidebar)
echo ==================================================
echo.

if not exist "%PYTHON%" (
    echo [ERROR] Python not found at: %PYTHON%
    pause
    exit /b 1
)

if not exist "%SERVER%" (
    echo [ERROR] gui_server.py not found at: %SERVER%
    pause
    exit /b 1
)

echo [INFO] Starting LLM Orchestra Server in a new window...
start "LLM Orchestra Server" /D "%AGENT_DIR%" "%PYTHON%" "%SERVER%"

echo.
echo [OK] Server launched. 
echo Leave the "LLM Orchestra Server" window open while working in VS Code.
timeout /t 3 /nobreak >nul
exit /b 0