"""Tests for the local, side-effect-free diagnostic CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import cli
from src.core import database
from src.core.config import Settings
from src.platform.example_run import ExampleRunResult
from src.platform.models import Automation, Run, RunStatus
from src.platform.run_service import get_or_create_automation, get_or_create_run


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


def test_run_example_json_reports_idempotent_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def execute(_key: str) -> ExampleRunResult:
        return ExampleRunResult(
            automation_id=1,
            run_id=2,
            step_run_id=3,
            run_status="succeeded",
            step_status="succeeded",
            created=False,
            replayed=True,
        )

    monkeypatch.setattr(cli, "_execute_example_command", execute)
    exit_code = cli.main(
        ["run-example", "--idempotency-key", "smoke-001", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["run_id"] == 2
    assert payload["replayed"] is True


def test_run_example_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(_key: str) -> ExampleRunResult:
        raise RuntimeError("secret database details")

    monkeypatch.setattr(cli, "_execute_example_command", fail)
    exit_code = cli.main(["run-example", "--idempotency-key", "smoke-002"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "RuntimeError" in output
    assert "secret database details" not in output


def test_kill_switch_command_is_explicit_and_structured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}

    async def set_control(
        slug: str,
        *,
        active: bool,
        reason: str,
    ) -> cli.ControlCommandResult:
        received.update(slug=slug, active=active, reason=reason)
        return cli.ControlCommandResult(
            action="kill_switch_enabled",
            target_id=7,
            changed=True,
        )

    monkeypatch.setattr(cli, "_set_kill_switch_command", set_control)
    exit_code = cli.main(
        [
            "kill-switch",
            "--automation-slug",
            "platform-smoke",
            "--enable",
            "--reason",
            "maintenance",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert received == {
        "slug": "platform-smoke",
        "active": True,
        "reason": "maintenance",
    }
    assert payload == {
        "ok": True,
        "action": "kill_switch_enabled",
        "target_id": 7,
        "changed": True,
    }


def test_cancel_run_reports_idempotent_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def cancel(run_id: int, *, reason: str) -> cli.ControlCommandResult:
        assert run_id == 42
        assert reason == "operator request"
        return cli.ControlCommandResult(
            action="cancellation_requested",
            target_id=run_id,
            changed=False,
        )

    monkeypatch.setattr(cli, "_cancel_run_command", cancel)
    exit_code = cli.main(
        [
            "cancel-run",
            "--run-id",
            "42",
            "--reason",
            "operator request",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "sem alteração (idempotente)" in output


def test_control_command_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(
        _slug: str,
        *,
        active: bool,
        reason: str,
    ) -> cli.ControlCommandResult:
        raise RuntimeError(f"private {active} {reason}")

    monkeypatch.setattr(cli, "_set_kill_switch_command", fail)
    exit_code = cli.main(
        [
            "kill-switch",
            "--automation-slug",
            "platform-smoke",
            "--disable",
            "--reason",
            "sensitive detail",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "RuntimeError" in output
    assert "sensitive detail" not in output


async def test_control_command_helpers_persist_real_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "cli-controls.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        echo=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed_session:
        automation = (
            await get_or_create_automation(
                seed_session,
                slug="cli-controlled",
                name="CLI Controlled",
                owner="local-owner",
            )
        ).automation
        run = (
            await get_or_create_run(
                seed_session,
                automation=automation,
                idempotency_key="cli-controlled:run",
            )
        ).run
        await seed_session.commit()
        automation_id = automation.id
        run_id = run.id

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    kill_result = await cli._set_kill_switch_command(
        "cli-controlled",
        active=True,
        reason="integration test",
    )
    cancel_result = await cli._cancel_run_command(
        run_id,
        reason="integration test",
    )

    async with factory() as observer_session:
        stored_automation = await observer_session.get(Automation, automation_id)
        stored_run = await observer_session.get(Run, run_id)
    await engine.dispose()

    assert kill_result.changed is True
    assert cancel_result.changed is True
    assert stored_automation is not None
    assert stored_automation.kill_switch_active is True
    assert stored_run is not None
    assert stored_run.status == RunStatus.CANCELLED.value
