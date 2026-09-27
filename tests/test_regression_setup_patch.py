import subprocess
import sys
from pathlib import Path


def test_splunk_mcp_compatibility_patcher_is_idempotent(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(
        'import os\nfrom mcp.server.fastmcp import FastMCP\n'
        'mcp = FastMCP("x", description=os.getenv("SERVER_DESCRIPTION", "desc"))\n',
        encoding="utf-8",
    )
    patcher = Path("scripts/patch_splunk_mcp.py").resolve()
    subprocess.run([sys.executable, str(patcher), str(server)], check=True)
    once = server.read_text(encoding="utf-8")
    assert 'instructions=os.getenv("SERVER_DESCRIPTION"' in once
    assert 'description=os.getenv("SERVER_DESCRIPTION"' not in once
    subprocess.run([sys.executable, str(patcher), str(server)], check=True)
    twice = server.read_text(encoding="utf-8")
    assert twice == once


def test_setup_runs_only_ALR_tests_installs_async_and_finishes_live_preflight():
    setup = Path("setup-ALR.cmd").read_text(encoding="utf-8")
    requirements = Path("requirements-agent.txt").read_text(encoding="utf-8")
    pytest_ini = Path("pytest.ini").read_text(encoding="utf-8")
    assert "-m pytest tests -vv" in setup
    assert "visible per-test progress" in setup
    assert "pytest-asyncio" in requirements
    assert "tzdata" in requirements
    assert "testpaths = tests" in pytest_ini
    assert 'scripts\\check_credentials.py' in setup
    assert 'call "edit-credentials.cmd"' in setup
    assert 'scripts\\live_splunk_preflight.py' in setup
    assert "SETUP COMPLETE - ROLE MODELS, UNIT TESTS AND LIVE SPLUNK PREFLIGHT PASSED" in setup
    assert 'scripts\\ollama_role_preflight.py' in setup
    assert 'gpt-oss:20b' in setup
    assert 'granite4.2:8b' in setup


def test_mutable_credentials_can_be_edited_without_breaking_package_integrity_test():
    # The example shipped with the package must remain blank. The live credentials
    # file is intentionally mutable and must not make future unit-test reruns fail.
    example = Path("config/credentials.example.json").read_text(encoding="utf-8")
    assert '"splunk_username": ""' in example
    assert '"splunk_password": ""' in example
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "config/credentials.json" in gitignore


def test_source_scan_excludes_generated_runtime_and_has_no_embedded_windows_user_path():
    # Scan only source/package material. Do not scan .venv/runtime/cache because those
    # are generated on the user's machine and legitimately contain absolute paths.
    roots = [Path("commander_agent"), Path("scripts"), Path("tests"), Path("config")]
    files = []
    for root in roots:
        files.extend(p for p in root.rglob("*") if p.is_file())
    files.extend(
        p
        for p in [
            Path("README.md"),
            Path("setup-ALR.cmd"),
            Path("run-ALR.cmd"),
            Path("edit-credentials.cmd"),
            Path("ALR.py"),
            Path("requirements-agent.txt"),
            Path("pytest.ini"),
            Path(".gitignore"),
        ]
        if p.exists()
    )
    mutable = {Path("config/credentials.json")}
    forbidden = ("c:" + "\\users" + "\\").lower()
    for p in files:
        if p in mutable or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        text = p.read_text(encoding="utf-8", errors="ignore").lower()
        assert forbidden not in text, f"embedded user path found in {p}"


def test_setup_repairs_existing_venv_without_pip():
    setup = Path("setup-ALR.cmd").read_text(encoding="utf-8")
    assert '".venv\\Scripts\\python.exe" -m pip --version' in setup
    assert 'Existing .venv is incomplete' in setup
    assert 'rmdir /s /q ".venv"' in setup
    assert '-m ensurepip --upgrade' in setup
