@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup-ALR.cmd first.
  exit /b 1
)
".venv\Scripts\python.exe" scripts\frame_question.py %*
exit /b %ERRORLEVEL%
