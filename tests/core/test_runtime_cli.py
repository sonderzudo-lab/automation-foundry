"""Redacted command behavior for the Windows runtime supervisor CLI."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from src import runtime_cli
from src.core.config import Settings
from src.runtime.supervisor import RuntimeStatus


def test_runtime_status_json_contains_no_process_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runtime_cli, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        runtime_cli,
        "load_runtime_status",
        lambda _configuration: RuntimeStatus(
            running=True,
            services={"worker-io": "running", "postgres": "not-owned"},
        ),
    )

    assert runtime_cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "running": True,
        "services": {"postgres": "not-owned", "worker-io": "running"},
    }
    assert "pid" not in repr(payload)


def test_runtime_stop_signals_only_the_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        runtime_cli,
        "get_settings",
        Mock(side_effect=AssertionError("stop must not load configuration")),
    )
    signal_calls = 0

    def signal() -> bool:
        nonlocal signal_calls
        signal_calls += 1
        return True

    monkeypatch.setattr(runtime_cli, "signal_runtime_stop", signal)

    assert runtime_cli.main(["stop"]) == 0
    assert signal_calls == 1
    assert "shutdown requested" in capsys.readouterr().out


def test_runtime_start_unexpected_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runtime_cli, "get_settings", lambda: Settings(_env_file=None))

    def fail(_configuration: Settings) -> None:
        raise RuntimeError("postgresql://user:secret@private-host")

    monkeypatch.setattr(runtime_cli, "run_supervisor", fail)

    assert runtime_cli.main(["start"]) == 1
    error = capsys.readouterr().err
    assert "inspect local runtime logs" in error
    assert "secret" not in error
    assert "private-host" not in error
