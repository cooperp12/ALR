@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo ALR legacy compatibility - ISOLATED PYTHON ENVIRONMENT SETUP
echo ============================================================
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python launcher ^(py.exe^) was not found.
  echo Install Python 3.13 or 3.14 from python.org, then rerun this file.
  exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
  echo ERROR: Git was not found.
  echo Install Git for Windows, then rerun this file.
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip --version >nul 2>nul
  if errorlevel 1 (
    echo [1/11] Existing .venv is incomplete ^(pip missing/broken^); recreating it...
    rmdir /s /q ".venv"
  ) else (
    echo [1/11] Existing healthy .venv found.
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/11] Creating project-local Python virtual environment...
  py -3.14 -m venv .venv >nul 2>nul
  if errorlevel 1 py -3.13 -m venv .venv >nul 2>nul
  if errorlevel 1 py -3 -m venv .venv
  if errorlevel 1 (
    echo ERROR: Could not create .venv.
    exit /b 1
  )
)

set "PY=%CD%\.venv\Scripts\python.exe"

rem Some Python installs can create a venv without pip. Repair once before failing.
"%PY%" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo Repairing pip inside the new .venv...
  "%PY%" -m ensurepip --upgrade
  if errorlevel 1 (
    echo ERROR: Python virtual environment exists but pip could not be installed.
    exit /b 1
  )
)

echo [2/11] Upgrading pip tooling inside .venv...
"%PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1

echo [3/11] Installing ALR legacy compatibility Python dependencies inside .venv...
"%PY%" -m pip install -r requirements-agent.txt
if errorlevel 1 exit /b 1

if not exist "runtime\splunk-mcp-server2\python\server.py" (
  echo [4/11] Cloning Splunk MCP server into project runtime...
  if not exist runtime mkdir runtime
  git clone --depth 1 https://github.com/splunk/splunk-mcp-server2.git runtime\splunk-mcp-server2
  if errorlevel 1 (
    echo ERROR: Failed to clone Splunk MCP server.
    exit /b 1
  )
) else (
  echo [4/11] Splunk MCP source already present in runtime.
)

echo [5/11] Pinning the compatible MCP runtime inside the SAME .venv...
"%PY%" -m pip install "mcp[cli]==1.30.0"
if errorlevel 1 exit /b 1

echo [6/11] Installing and patching the project-local Splunk MCP server...
"%PY%" -m pip install -e "runtime\splunk-mcp-server2\python"
if errorlevel 1 exit /b 1
"%PY%" "scripts\patch_splunk_mcp.py" "runtime\splunk-mcp-server2\python\server.py"
if errorlevel 1 exit /b 1
"%PY%" -c "import sys; sys.path.insert(0, r'%CD%\runtime\splunk-mcp-server2\python'); import importlib.metadata; import server; print('MCP version:', importlib.metadata.version('mcp')); print('MCP/server import OK'); print('MCP object:', type(server.mcp).__name__)"
if errorlevel 1 (
  echo ERROR: Splunk MCP server import failed after compatibility setup.
  exit /b 1
)

if not exist "config\credentials.json" (
  copy /y "config\credentials.example.json" "config\credentials.json" >nul
)

echo [7/11] Verifying ALR legacy compatibility imports and skill registry...
"%PY%" -m compileall -q commander_agent scripts ALR.py
if errorlevel 1 exit /b 1
"%PY%" -c "import ollama; from mcp import ClientSession, StdioServerParameters; import commander_agent.config as c; from commander_agent.skills.registry import SKILLS; from commander_agent.state import evidence_store; from commander_agent.state.release import canonical_release_status; from commander_agent.core import measurement_resolution; from commander_agent.core.q6_anomaly_review import review_q6_anomaly; from commander_agent.skills.bidirectional_timeframe.scripts.timeline_engine import query_timeline; from commander_agent.semantic.frame import heuristic_frame; from commander_agent.semantic.binding import resolve_semantic_bindings; from commander_agent.semantic.query_policy import semantic_query_policy_violations; from commander_agent.semantic.contracts import build_source_contract, build_extraction_contract; from commander_agent.state.extraction import active_extraction_contract, derive_extraction_candidate, verify_extraction_candidate; from commander_agent.state.evidence import current_progress_version; from commander_agent.state.sequence import build_validated_sequence_state; from commander_agent.evaluation.isolation import prepare_benchmark_isolation; assert 'result_semantics_recovery' in SKILLS; assert 'measurement_semantics_resolution' in SKILLS; assert 'investigation_frame' in SKILLS; assert 'semantic_layer' in SKILLS; assert 'temporal_pattern_analysis' in SKILLS; assert 'investigation_supervisor' in SKILLS; assert 'incident_sequence_state' in SKILLS; assert 'cloudtrail_identity_expansion' in SKILLS; assert 'ami_release_resolution' in SKILLS; import polars, numpy, matplotlib, tzdata; print('ALR legacy compatibility import test OK'); print('Python:', __import__('sys').executable); print('Credentials file:', c.CREDENTIALS_FILE); print('Investigator model:', c.INVESTIGATOR_MODEL); print('Supervisor model:', c.SUPERVISOR_MODEL); print('Durable evidence store: present'); print('Canonical release gate: present'); print('Measurement semantics resolver: present'); print('Investigation supervisor: present'); print('Universal semantic query policy: present'); print('Source contracts: present'); print('Extraction contracts: present'); print('Deterministic direct extraction resolver: present'); print('Analytical progress-aware stagnation guard: present'); print('Benchmark mode: Q1-Q3 then Q6 with carried validated state'); print('Q6 sequence resolver: present'); print('CloudTrail identity expansion skill: present'); print('Generic sequence-state promotion: present'); print('Adaptive temporal scope: present'); print('Relationship confidence/provenance: present'); print('Benchmark fresh-state isolation: present'); print('Tiered external enrichment: present'); print('Q6 stage diagnostics: present'); print('Q6 anomaly Worker/Supervisor review: present'); print('Chronology-safe timeline query: present'); print('Raw boundary verification path: present'); print('Deterministic official Ubuntu codename route: present')"
if errorlevel 1 exit /b 1

echo [8/11] Verifying both Ollama role models and structured output...
"%PY%" "scripts\ollama_role_preflight.py"
if errorlevel 1 (
  echo ERROR: Ollama role-model preflight failed.
  echo Required models: gpt-oss:20b and granite4.2:8b
  exit /b 1
)

echo [9/11] Running ALR legacy compatibility unit tests with visible per-test progress...
echo       This intentionally prints each test name and percentage so a slower regression cannot look stalled.
if exist ".pytest_tmp" rmdir /s /q ".pytest_tmp" >nul 2>nul
"%PY%" -m pytest tests -vv --tb=short --durations=10 --basetemp=.pytest_tmp
set "TEST_RC=%ERRORLEVEL%"
if exist ".pytest_tmp" rmdir /s /q ".pytest_tmp" >nul 2>nul
if not "%TEST_RC%"=="0" (
  echo ERROR: ALR legacy compatibility unit tests failed.
  exit /b 1
)

echo [10/11] Checking Splunk credentials...
"%PY%" "scripts\check_credentials.py"
if errorlevel 1 (
  echo.
  echo Splunk credentials are missing or incomplete.
  echo Opening the local credential file now.
  call "edit-credentials.cmd"
  echo.
  echo Re-checking credentials after the editor closed...
  "%PY%" "scripts\check_credentials.py"
  if errorlevel 1 (
    echo ERROR: Splunk credentials are still missing, incomplete, or invalid.
    echo Setup cannot complete the live Splunk test until they are supplied.
    exit /b 1
  )
)

echo [11/11] Running live MCP + Splunk + BOTSv3 preflight...
"%PY%" "scripts\live_splunk_preflight.py"
if errorlevel 1 (
  echo ERROR: Live Splunk preflight failed.
  echo Fix the local Docker Splunk credentials, permissions, or BOTSv3 dataset and rerun setup-ALR.cmd.
  exit /b 1
)

echo.
echo ============================================================
echo SETUP COMPLETE - ROLE MODELS, UNIT TESTS AND LIVE SPLUNK PREFLIGHT PASSED
echo ============================================================
echo Python environment:
echo   %CD%\.venv

echo.
echo Splunk MCP source:
echo   %CD%\runtime\splunk-mcp-server2

echo.
echo Credentials file:
echo   %CD%\config\credentials.json

echo.
echo Run:
echo   run-ALR.cmd

echo.
endlocal
