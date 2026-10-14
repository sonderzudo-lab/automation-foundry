"""Exercise the scheduled-task path resolver with the real Settings parser."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

POWERSHELL = shutil.which("powershell.exe")
HELPER = Path(__file__).resolve().parents[2] / "scripts/windows/storage-paths.ps1"
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell required"
)


def _resolve(root: Path, *, override: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key.upper() != "STORAGE_ROOT"}
    env.update(AF_TEST_ROOT=str(root), AF_TEST_PYTHON=sys.executable, AF_TEST_HELPER=str(HELPER))
    if override is not None:
        env["STORAGE_ROOT"] = override
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'; . $env:AF_TEST_HELPER; "
            "Get-FoundryStorageRoot -RepositoryRoot $env:AF_TEST_ROOT "
            "-PythonExecutable $env:AF_TEST_PYTHON",
        ],
        env=env,
        cwd=root.parent,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("dotenv", [None, 'STORAGE_ROOT="./storage" # local\n'])
def test_default_and_relative_paths_use_repo_not_cwd(tmp_path: Path, dotenv: str | None) -> None:
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    result = _resolve(tmp_path)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == tmp_path / "storage"


def test_absolute_dotenv_path_with_spaces(tmp_path: Path) -> None:
    target = tmp_path.parent / "other disk storage"
    (tmp_path / ".env").write_text(f"STORAGE_ROOT='{target}' # moved\n", encoding="utf-8")
    result = _resolve(tmp_path)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == target


@pytest.mark.parametrize("override", ["./override storage", ""])
def test_environment_precedence_matches_application(tmp_path: Path, override: str) -> None:
    (tmp_path / ".env").write_text("STORAGE_ROOT=./dotenv-storage\n", encoding="utf-8")
    result = _resolve(tmp_path, override=override)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == tmp_path / (override or "dotenv-storage")


def test_invalid_settings_fail_without_exposing_value(tmp_path: Path) -> None:
    secret_marker = "private-invalid-configuration"
    (tmp_path / ".env").write_text(f"DASHBOARD_PORT={secret_marker}\n", encoding="utf-8")
    result = _resolve(tmp_path)
    assert result.returncode != 0
    assert "Storage configuration could not be loaded" in result.stderr
    assert secret_marker not in result.stdout + result.stderr
