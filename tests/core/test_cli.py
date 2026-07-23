"""Tests for the local, side-effect-free diagnostic CLI."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from src import cli
from src.core.config import Settings


@pytest.fixture
def local_settings() -> Settings:
    return Settings(_env_file=None)


def test_doctor_static_checks_pass_without_services(
    local_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.metadata, "version", lambda _name: "0.1.0")

    results = cli.run_doctor(local_settings)

    assert not [result for result in results if result.status == "fail"]
    assert {result.name for result in results if result.status == "skip"} == {
        "redis_service",
        "ollama_service",
    }


def test_doctor_rejects_non_loopback_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.metadata, "version", lambda _name: "0.1.0")
    settings = Settings(
        _env_file=None,
        ollama_base_url="https://models.example.com/v1",
    )

    results = cli.run_doctor(settings)

    failure = next(result for result in results if result.name == "ollama_config")
    assert failure.status == "fail"
    assert "models.example.com" not in failure.detail


def test_doctor_rejects_unsupported_endpoint_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.metadata, "version", lambda _name: "0.1.0")
    settings = Settings(
        _env_file=None,
        redis_url="http://127.0.0.1:6379/0",
    )

    results = cli.run_doctor(settings)

    failure = next(result for result in results if result.name == "redis_config")
    assert failure.status == "fail"
    assert failure.detail == "protocolo não suportado"


def test_service_checks_probe_only_configured_loopback_ports(
    local_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.metadata, "version", lambda _name: "0.1.0")
    connection = Mock()
    create_connection = Mock(return_value=connection)
    monkeypatch.setattr(cli.socket, "create_connection", create_connection)

    results = cli.run_doctor(local_settings, check_services=True)

    assert create_connection.call_count == 2
    create_connection.assert_any_call(("127.0.0.1", 6379), timeout=0.5)
    create_connection.assert_any_call(("127.0.0.1", 11434), timeout=0.5)
    assert connection.close.call_count == 2
    assert not [result for result in results if result.status == "fail"]


def test_main_json_output_is_structured_and_redacted(
    local_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: local_settings)
    monkeypatch.setattr(cli.metadata, "version", lambda _name: "0.1.0")

    exit_code = cli.main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["summary"] == {"pass": 5, "fail": 0, "skip": 2}
    assert "sqlite+aiosqlite" not in json.dumps(payload)


def test_main_returns_failure_when_local_services_are_unavailable(
    local_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: local_settings)
    monkeypatch.setattr(cli.metadata, "version", lambda _name: "0.1.0")
    monkeypatch.setattr(
        cli.socket,
        "create_connection",
        Mock(side_effect=ConnectionRefusedError),
    )

    exit_code = cli.main(["doctor", "--services"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Resultado: FAIL" in output
    assert "indisponível em loopback" in output
