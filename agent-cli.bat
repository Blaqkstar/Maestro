@echo off
REM Hardcode the path to the venv python to avoid any PATH issues
set "PYTHON_EXE=H:\AI\Agents\SWE\.venv\Scripts\python.exe"
set "AGENT_SCRIPT=H:\AI\Agents\SWE\agent.py"

REM %* passes all arguments from Cline directly to the Python script
"%PYTHON_EXE%" "%AGENT_SCRIPT%" %*