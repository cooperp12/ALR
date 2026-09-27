@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Project virtual environment does not exist.
  echo Run setup-ALR.cmd first.
  exit /b 1
)

".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo ERROR: Project virtual environment is incomplete ^(pip missing/broken^).
  echo Run setup-ALR.cmd to repair it.
  exit /b 1
)

if not exist "runtime\splunk-mcp-server2\python\server.py" (
  echo ERROR: Project-local Splunk MCP server is missing.
  echo Run setup-ALR.cmd first.
  exit /b 1
)

if not exist "config\credentials.json" (
  echo ERROR: config\credentials.json is missing.
  echo Run setup-ALR.cmd to create and validate it.
  exit /b 1
)

set "ALR legacy compatibility_FORCE_FRESH=1"
"%CD%\.venv\Scripts\python.exe" ALR.py
set "ALR legacy compatibility_FORCE_FRESH="
endlocal
