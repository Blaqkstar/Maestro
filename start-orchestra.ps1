# Navigate to the Agent directory
Set-Location -Path "H:\AI\Agents\SWE"

# Activate the virtual environment
. .\.venv\Scripts\Activate.ps1

# Start the server
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Starting LLM Orchestra Server (Cline Mode)" -ForegroundColor Cyan
Write-Host "  Endpoint: http://localhost:8765/v1" -ForegroundColor Green
Write-Host "  Press Ctrl+C in this window to stop the server." -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Cyan

llm-orc serve --no-backend