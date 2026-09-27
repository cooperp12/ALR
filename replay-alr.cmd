@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup-ALR.cmd first.
  exit /b 1
)
if exist ".pytest_replay_tmp" rmdir /s /q ".pytest_replay_tmp" >nul 2>nul
".venv\Scripts\python.exe" -m pytest tests\test_ALR_q6_sequence.py tests\test_ALR_q6_failures.py tests\test_ALR_contamination_boundary.py tests\test_regression_semantic_relationship_guard.py tests\test_regression_evidence_store.py tests\test_regression_candidate_handoff.py -q --basetemp=.pytest_replay_tmp
set "RC=%ERRORLEVEL%"
if exist ".pytest_replay_tmp" rmdir /s /q ".pytest_replay_tmp" >nul 2>nul
exit /b %RC%
