"""Tests for the local, side-effect-free diagnostic CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import cli
from src.core import database
from src.core.config import Settings
from src.platform.dispatch_service import DispatchPreparationResult
from src.platform.example_run import ExampleRunResult
from src.platform.models import (
    Alert,
    AlertEventType,
    AlertStatus,
    Approval,
    ApprovalStatus,
    Artifact,
    Automation,
    MetricPoint,
    QueueClass,
    Run,
    RunStatus,
    Schedule,
    ScheduleStatus,
)
from src.platform.models import (
    AlertEvent as PlatformAlertEvent,
)
from src.platform.run_service import (
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)


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
        "database_service",
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
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["summary"] == {"pass": 5, "fail": 0, "skip": 3}
    assert "sqlite+aiosqlite" not in json.dumps(payload)


def test_doctor_probes_configured_postgresql_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.metadata, "version", lambda _name: "0.1.0")
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql+asyncpg://foundry:private-password@127.0.0.1:55432/"
            "automation_foundry"
        ),
    )
    connection = Mock()
    create_connection = Mock(return_value=connection)
    monkeypatch.setattr(cli.socket, "create_connection", create_connection)

    results = cli.run_doctor(settings, check_services=True)

    create_connection.assert_any_call(("127.0.0.1", 55432), timeout=0.5)
    database_check = next(
        result for result in results if result.name == "database_service"
    )
    assert database_check.status == "pass"
    assert "private-password" not in " ".join(result.detail for result in results)


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


def test_enqueue_run_reports_stable_delivery(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def enqueue(slug: str, key: str) -> DispatchPreparationResult:
        assert (slug, key) == ("platform-smoke", "queued-001")
        return DispatchPreparationResult(
            run_id=4,
            dispatch_id=5,
            delivery_id="delivery-001",
            queue=QueueClass.IO,
            created=True,
        )

    monkeypatch.setattr(cli, "_enqueue_run_command", enqueue)
    exit_code = cli.main(
        [
            "enqueue-run",
            "--automation-slug",
            "platform-smoke",
            "--idempotency-key",
            "queued-001",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["dispatch_id"] == 5
    assert payload["queue"] == "io"
    assert payload["delivery_id"] == "delivery-001"


def test_kill_switch_command_is_explicit_and_structured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}

    async def set_control(
        slug: str,
        *,
        active: bool,
        actor: str,
        reason: str,
    ) -> cli.ControlCommandResult:
        received.update(slug=slug, active=active, actor=actor, reason=reason)
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
            "--actor",
            "local-owner",
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
        "actor": "local-owner",
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
        actor: str,
        reason: str,
    ) -> cli.ControlCommandResult:
        raise RuntimeError(f"private {active} {actor} {reason}")

    monkeypatch.setattr(cli, "_set_kill_switch_command", fail)
    exit_code = cli.main(
        [
            "kill-switch",
            "--automation-slug",
            "platform-smoke",
            "--disable",
            "--actor",
            "local-owner",
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
        actor="local-owner",
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


def test_request_approval_command_renders_pending_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def request(
        run_id: int,
        *,
        idempotency_key: str,
        action: str,
        summary: str,
        review: str,
    ) -> cli.ApprovalCommandResult:
        assert (run_id, idempotency_key, action, summary) == (
            9,
            "publish:9",
            "publish",
            "Review private upload",
        )
        assert review == '{"artifact":"Video #9"}'
        return cli.ApprovalCommandResult(
            approval_id=11,
            run_id=run_id,
            status="pending",
            changed=True,
        )

    monkeypatch.setattr(cli, "_request_approval_command", request)
    exit_code = cli.main(
        [
            "request-approval",
            "--run-id",
            "9",
            "--idempotency-key",
            "publish:9",
            "--action",
            "publish",
            "--summary",
            "Review private upload",
            "--review",
            '{"artifact":"Video #9"}',
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["approval_id"] == 11
    assert payload["status"] == "pending"


def test_decide_approval_command_requires_explicit_decision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def decide(
        approval_id: int,
        *,
        approve: bool,
        actor: str,
        reason: str,
    ) -> cli.ApprovalCommandResult:
        assert (approval_id, approve, actor, reason) == (
            11,
            False,
            "local-owner",
            "needs revision",
        )
        return cli.ApprovalCommandResult(
            approval_id=approval_id,
            run_id=9,
            status="rejected",
            changed=True,
        )

    monkeypatch.setattr(cli, "_decide_approval_command", decide)
    exit_code = cli.main(
        [
            "decide-approval",
            "--approval-id",
            "11",
            "--reject",
            "--actor",
            "local-owner",
            "--reason",
            "needs revision",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "rejected" in output


async def test_approval_command_helpers_persist_real_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "cli-approvals.db"
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
                slug="cli-approval",
                name="CLI Approval",
                owner="local-owner",
            )
        ).automation
        run = (
            await get_or_create_run(
                seed_session,
                automation=automation,
                idempotency_key="cli-approval:run",
                input_payload={"artifact_id": 5},
            )
        ).run
        await transition_run(seed_session, run, RunStatus.RUNNING)
        await seed_session.commit()
        run_id = run.id

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    requested = await cli._request_approval_command(
        run_id,
        idempotency_key="cli-approval:publish",
        action="publish",
        summary="Review local artifact",
        review='{"artifact":"Video #5"}',
    )
    decided = await cli._decide_approval_command(
        requested.approval_id,
        approve=True,
        actor="local-owner",
        reason="review passed",
    )

    async with factory() as observer_session:
        stored_run = await observer_session.get(Run, run_id)
        stored_approval = await observer_session.get(
            Approval,
            requested.approval_id,
        )
    await engine.dispose()

    assert requested.status == ApprovalStatus.PENDING.value
    assert decided.status == ApprovalStatus.APPROVED.value
    assert stored_run is not None
    assert stored_run.status == RunStatus.RUNNING.value
    assert stored_approval is not None
    assert stored_approval.decided_by == "local-owner"


def test_register_artifact_command_renders_redacted_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}

    async def register(
        run_id: int,
        *,
        step_run_id: int | None,
        idempotency_key: str,
        artifact_type: str,
        file_path: str,
        media_type: str,
        origin: str,
        sensitivity: str,
        retention_days: int | None,
        expected_sha256: str | None,
    ) -> cli.ArtifactCommandResult:
        received.update(
            run_id=run_id,
            step_run_id=step_run_id,
            idempotency_key=idempotency_key,
            artifact_type=artifact_type,
            file_path=file_path,
            media_type=media_type,
            origin=origin,
            sensitivity=sensitivity,
            retention_days=retention_days,
            expected_sha256=expected_sha256,
        )
        return cli.ArtifactCommandResult(
            artifact_id=17,
            run_id=run_id,
            sha256="a" * 64,
            size_bytes=42,
            created=True,
        )

    monkeypatch.setattr(cli, "_register_artifact_command", register)
    exit_code = cli.main(
        [
            "register-artifact",
            "--run-id",
            "9",
            "--idempotency-key",
            "run-9:script",
            "--artifact-type",
            "script",
            "--path",
            "run-9/script.json",
            "--media-type",
            "application/json",
            "--origin",
            "content-engine",
            "--sensitivity",
            "confidential",
            "--retention-days",
            "30",
            "--json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert received["file_path"] == "run-9/script.json"
    assert received["sensitivity"] == "confidential"
    assert received["retention_days"] == 30
    assert payload == {
        "ok": True,
        "artifact_id": 17,
        "run_id": 9,
        "sha256": "a" * 64,
        "size_bytes": 42,
        "created": True,
    }
    assert "run-9/script.json" not in output


def test_register_artifact_failure_does_not_echo_private_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = "C:/private/customer-name/secret.txt"

    async def fail(*_args: object, **_kwargs: object) -> cli.ArtifactCommandResult:
        raise RuntimeError(private_path)

    monkeypatch.setattr(cli, "_register_artifact_command", fail)
    exit_code = cli.main(
        [
            "register-artifact",
            "--run-id",
            "9",
            "--idempotency-key",
            "run-9:secret",
            "--artifact-type",
            "secret",
            "--path",
            private_path,
            "--media-type",
            "text/plain",
            "--origin",
            "local",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "RuntimeError" in output
    assert private_path not in output


async def test_register_artifact_helper_persists_real_local_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "cli-artifacts.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    artifact_path = storage_root / "result.txt"
    artifact_path.write_text("verified local output", encoding="utf-8")
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
                slug="cli-artifact",
                name="CLI Artifact",
                owner="local-owner",
            )
        ).automation
        run = (
            await get_or_create_run(
                seed_session,
                automation=automation,
                idempotency_key="cli-artifact:run",
            )
        ).run
        await seed_session.commit()
        run_id = run.id

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(_env_file=None, storage_root=str(storage_root)),
    )
    result = await cli._register_artifact_command(
        run_id,
        step_run_id=None,
        idempotency_key="cli-artifact:result",
        artifact_type="result",
        file_path="result.txt",
        media_type="text/plain",
        origin="local-cli-test",
        sensitivity="internal",
        retention_days=None,
        expected_sha256=None,
    )

    async with factory() as observer_session:
        stored_artifact = await observer_session.get(Artifact, result.artifact_id)
    await engine.dispose()

    assert result.created is True
    assert stored_artifact is not None
    assert stored_artifact.relative_path == "result.txt"
    assert stored_artifact.size_bytes == len("verified local output")


def test_create_schedule_command_starts_disabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}

    async def create(
        automation_slug: str,
        *,
        name: str,
        cron_expression: str,
        timezone_name: str,
        actor: str,
        reason: str,
        allow_overlap: bool,
        misfire_grace_seconds: int,
    ) -> cli.ScheduleCommandResult:
        received.update(
            automation_slug=automation_slug,
            name=name,
            cron_expression=cron_expression,
            timezone_name=timezone_name,
            actor=actor,
            reason=reason,
            allow_overlap=allow_overlap,
            misfire_grace_seconds=misfire_grace_seconds,
        )
        return cli.ScheduleCommandResult(
            schedule_id=21,
            automation_id=7,
            status="disabled",
            next_run_at=None,
            changed=True,
        )

    monkeypatch.setattr(cli, "_create_schedule_command", create)
    exit_code = cli.main(
        [
            "create-schedule",
            "--automation-slug",
            "daily-reports",
            "--name",
            "morning-report",
            "--cron",
            "0 9 * * *",
            "--timezone",
            "America/Sao_Paulo",
            "--actor",
            "local-owner",
            "--reason",
            "reviewed recurring report",
            "--misfire-grace-seconds",
            "120",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert received["cron_expression"] == "0 9 * * *"
    assert received["timezone_name"] == "America/Sao_Paulo"
    assert received["allow_overlap"] is False
    assert received["misfire_grace_seconds"] == 120
    assert payload["status"] == "disabled"
    assert payload["next_run_at"] is None


def test_set_schedule_command_requires_explicit_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def change(
        schedule_id: int,
        *,
        enabled: bool,
        actor: str,
        reason: str,
    ) -> cli.ScheduleCommandResult:
        assert (schedule_id, enabled, actor, reason) == (
            21,
            True,
            "local-owner",
            "start reviewed recurring work",
        )
        return cli.ScheduleCommandResult(
            schedule_id=schedule_id,
            automation_id=7,
            status="enabled",
            next_run_at="2026-07-24T12:00:00Z",
            changed=True,
        )

    monkeypatch.setattr(cli, "_set_schedule_command", change)
    exit_code = cli.main(
        [
            "set-schedule",
            "--schedule-id",
            "21",
            "--enable",
            "--actor",
            "local-owner",
            "--reason",
            "start reviewed recurring work",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "enabled" in output
    assert "2026-07-24T12:00:00Z" in output


def test_create_experiment_command_records_explicit_definition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}

    async def create(
        automation_slug: str,
        **kwargs: object,
    ) -> cli.ExperimentCommandResult:
        received.update(automation_slug=automation_slug, **kwargs)
        return cli.ExperimentCommandResult(
            experiment_id=31,
            automation_id=7,
            key="headline-v2",
            status="draft",
            changed=True,
        )

    monkeypatch.setattr(cli, "_create_experiment_command", create)
    exit_code = cli.main(
        [
            "create-experiment",
            "--automation-slug",
            "platform-smoke",
            "--key",
            "headline-v2",
            "--name",
            "Headline V2",
            "--hypothesis",
            "Clearer wording improves completion.",
            "--primary-metric",
            "content.completion_rate",
            "--unit",
            "ratio",
            "--control",
            "current",
            "--candidate",
            "clearer-v2",
            "--actor",
            "local-owner",
            "--reason",
            "register reviewed hypothesis",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert received["automation_slug"] == "platform-smoke"
    assert received["primary_metric"] == "content.completion_rate"
    assert received["control"] == "current"
    assert received["candidate"] == "clearer-v2"
    assert payload["status"] == "draft"


def test_set_experiment_command_maps_explicit_transition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def change(
        experiment_id: int,
        *,
        target: str,
        actor: str,
        reason: str,
    ) -> cli.ExperimentCommandResult:
        assert (experiment_id, target, actor, reason) == (
            31,
            "running",
            "local-owner",
            "start reviewed measurement",
        )
        return cli.ExperimentCommandResult(
            experiment_id=experiment_id,
            automation_id=7,
            key="headline-v2",
            status=target,
            changed=True,
        )

    monkeypatch.setattr(cli, "_set_experiment_command", change)
    exit_code = cli.main(
        [
            "set-experiment",
            "--experiment-id",
            "31",
            "--start",
            "--actor",
            "local-owner",
            "--reason",
            "start reviewed measurement",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "running"


async def test_schedule_command_helpers_persist_real_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "cli-schedules.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        echo=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed_session:
        await get_or_create_automation(
            seed_session,
            slug="cli-schedule",
            name="CLI Schedule",
            owner="local-owner",
        )
        await seed_session.commit()

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    created = await cli._create_schedule_command(
        "cli-schedule",
        name="morning-report",
        cron_expression="0 9 * * *",
        timezone_name="America/Sao_Paulo",
        actor="local-owner",
        reason="integration test registration",
        allow_overlap=False,
        misfire_grace_seconds=300,
    )
    enabled = await cli._set_schedule_command(
        created.schedule_id,
        enabled=True,
        actor="local-owner",
        reason="integration test enable",
    )

    async with factory() as observer_session:
        stored_schedule = await observer_session.get(Schedule, created.schedule_id)
    await engine.dispose()

    assert created.status == ScheduleStatus.DISABLED.value
    assert created.next_run_at is None
    assert enabled.status == ScheduleStatus.ENABLED.value
    assert enabled.next_run_at is not None
    assert stored_schedule is not None
    assert stored_schedule.status == ScheduleStatus.ENABLED.value
    assert stored_schedule.next_run_at is not None


def test_record_metric_command_is_structured_and_precise(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}

    async def record(
        automation_slug: str,
        *,
        run_id: int | None,
        step_run_id: int | None,
        idempotency_key: str,
        name: str,
        kind: str,
        value: str,
        unit: str,
        source: str,
        confidence: str | None,
        observed_at: str | None,
    ) -> cli.MetricCommandResult:
        received.update(
            automation_slug=automation_slug,
            run_id=run_id,
            step_run_id=step_run_id,
            idempotency_key=idempotency_key,
            name=name,
            kind=kind,
            value=value,
            unit=unit,
            source=source,
            confidence=confidence,
            observed_at=observed_at,
        )
        return cli.MetricCommandResult(
            metric_point_id=31,
            automation_id=7,
            run_id=9,
            step_run_id=11,
            name="step.duration",
            kind="duration",
            value="12.345",
            unit="s",
            observed_at="2026-07-23T12:30:00Z",
            created=True,
        )

    monkeypatch.setattr(cli, "_record_metric_command", record)
    exit_code = cli.main(
        [
            "record-metric",
            "--automation-slug",
            "content-engine",
            "--run-id",
            "9",
            "--step-run-id",
            "11",
            "--idempotency-key",
            "run-9:step-11:duration",
            "--name",
            "step.duration",
            "--kind",
            "duration",
            "--value",
            "12.345",
            "--unit",
            "s",
            "--source",
            "task-runner",
            "--confidence",
            "0.99",
            "--observed-at",
            "2026-07-23T09:30:00-03:00",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert received["value"] == "12.345"
    assert received["observed_at"] == "2026-07-23T09:30:00-03:00"
    assert payload == {
        "ok": True,
        "metric_point_id": 31,
        "automation_id": 7,
        "run_id": 9,
        "step_run_id": 11,
        "name": "step.duration",
        "kind": "duration",
        "value": "12.345",
        "unit": "s",
        "observed_at": "2026-07-23T12:30:00Z",
        "created": True,
    }


async def test_record_metric_helper_persists_real_local_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "cli-metrics.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        echo=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed_session:
        await get_or_create_automation(
            seed_session,
            slug="cli-metric",
            name="CLI Metric",
            owner="local-owner",
        )
        await seed_session.commit()

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    result = await cli._record_metric_command(
        "cli-metric",
        run_id=None,
        step_run_id=None,
        idempotency_key="cli-metric:queue-depth:1",
        name="queue.depth",
        kind="gauge",
        value="3.250",
        unit="tasks",
        source="local-control-plane",
        confidence=None,
        observed_at="2026-07-23T09:30:00-03:00",
    )

    async with factory() as observer_session:
        stored_metric = await observer_session.get(MetricPoint, result.metric_point_id)
    await engine.dispose()

    assert result.created is True
    assert result.value == "3.25"
    assert result.observed_at == "2026-07-23T12:30:00Z"
    assert stored_metric is not None
    assert stored_metric.name == "queue.depth"
    assert stored_metric.source == "local-control-plane"


def test_record_alert_command_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}
    private_summary = "Customer Alpha connector failed with private details."

    async def record(
        automation_slug: str,
        *,
        run_id: int | None,
        step_run_id: int | None,
        metric_point_id: int | None,
        deduplication_key: str,
        idempotency_key: str,
        title: str,
        summary: str,
        severity: str,
        source: str,
        observed_at: str | None,
    ) -> cli.AlertCommandResult:
        received.update(
            automation_slug=automation_slug,
            run_id=run_id,
            step_run_id=step_run_id,
            metric_point_id=metric_point_id,
            deduplication_key=deduplication_key,
            idempotency_key=idempotency_key,
            title=title,
            summary=summary,
            severity=severity,
            source=source,
            observed_at=observed_at,
        )
        return cli.AlertCommandResult(
            alert_id=41,
            automation_id=7,
            status="open",
            severity="critical",
            occurrence_count=2,
            last_seen_at="2026-07-23T12:30:00Z",
            changed=True,
        )

    monkeypatch.setattr(cli, "_record_alert_command", record)
    exit_code = cli.main(
        [
            "record-alert",
            "--automation-slug",
            "content-engine",
            "--run-id",
            "9",
            "--step-run-id",
            "11",
            "--metric-point-id",
            "31",
            "--deduplication-key",
            "connector:youtube:offline",
            "--idempotency-key",
            "connector:youtube:offline:20260723T1230",
            "--title",
            "Connector is offline",
            "--summary",
            private_summary,
            "--severity",
            "critical",
            "--source",
            "connector-monitor",
            "--observed-at",
            "2026-07-23T09:30:00-03:00",
            "--json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert received["summary"] == private_summary
    assert received["metric_point_id"] == 31
    assert payload == {
        "ok": True,
        "alert_id": 41,
        "automation_id": 7,
        "status": "open",
        "severity": "critical",
        "occurrence_count": 2,
        "last_seen_at": "2026-07-23T12:30:00Z",
        "changed": True,
    }
    assert private_summary not in output


def test_set_alert_command_requires_explicit_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def change(
        alert_id: int,
        *,
        acknowledge: bool,
        actor: str,
        reason: str,
    ) -> cli.AlertCommandResult:
        assert (alert_id, acknowledge, actor, reason) == (
            41,
            True,
            "local-owner",
            "investigating locally",
        )
        return cli.AlertCommandResult(
            alert_id=alert_id,
            automation_id=7,
            status="acknowledged",
            severity="critical",
            occurrence_count=2,
            last_seen_at="2026-07-23T12:30:00Z",
            changed=True,
        )

    monkeypatch.setattr(cli, "_set_alert_command", change)
    exit_code = cli.main(
        [
            "set-alert",
            "--alert-id",
            "41",
            "--acknowledge",
            "--actor",
            "local-owner",
            "--reason",
            "investigating locally",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "acknowledged/critical" in output
    assert "ocorrencias=2" in output


def test_record_alert_failure_does_not_echo_private_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_summary = "Customer Alpha private connector payload."

    async def fail(*_args: object, **_kwargs: object) -> cli.AlertCommandResult:
        raise RuntimeError(private_summary)

    monkeypatch.setattr(cli, "_record_alert_command", fail)
    exit_code = cli.main(
        [
            "record-alert",
            "--automation-slug",
            "content-engine",
            "--deduplication-key",
            "connector:private",
            "--idempotency-key",
            "connector:private:1",
            "--title",
            "Connector failure",
            "--summary",
            private_summary,
            "--severity",
            "error",
            "--source",
            "connector-monitor",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "RuntimeError" in output
    assert private_summary not in output


async def test_alert_command_helpers_persist_real_local_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "cli-alerts.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        echo=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed_session:
        await get_or_create_automation(
            seed_session,
            slug="cli-alert",
            name="CLI Alert",
            owner="local-owner",
        )
        await seed_session.commit()

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    recorded = await cli._record_alert_command(
        "cli-alert",
        run_id=None,
        step_run_id=None,
        metric_point_id=None,
        deduplication_key="service:redis:offline",
        idempotency_key="service:redis:offline:1",
        title="Redis is offline",
        summary="The local broker did not accept a connection.",
        severity="error",
        source="service-health",
        observed_at=None,
    )
    acknowledged = await cli._set_alert_command(
        recorded.alert_id,
        acknowledge=True,
        actor="local-owner",
        reason="investigating the local service",
    )
    resolved = await cli._set_alert_command(
        recorded.alert_id,
        acknowledge=False,
        actor="local-owner",
        reason="local service recovered",
    )

    async with factory() as observer_session:
        stored_alert = await observer_session.get(Alert, recorded.alert_id)
        event_types = list(
            await observer_session.scalars(
                select(PlatformAlertEvent.event_type).where(
                    PlatformAlertEvent.alert_id == recorded.alert_id
                ).order_by(PlatformAlertEvent.id)
            )
        )
    await engine.dispose()

    assert recorded.changed is True
    assert acknowledged.status == AlertStatus.ACKNOWLEDGED.value
    assert resolved.status == AlertStatus.RESOLVED.value
    assert stored_alert is not None
    assert stored_alert.status == AlertStatus.RESOLVED.value
    assert event_types == [
        AlertEventType.OPENED.value,
        AlertEventType.ACKNOWLEDGED.value,
        AlertEventType.RESOLVED.value,
    ]


def test_record_ledger_command_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}
    private_category = "private-customer-attribution"
    private_source = "private invoice reference"
    private_key = "private-ledger-key"

    async def record(
        automation_slug: str,
        *,
        run_id: int | None,
        step_run_id: int | None,
        metric_point_id: int | None,
        idempotency_key: str,
        entry_type: str,
        category: str,
        amount: str,
        currency: str,
        source: str,
        confidence: str | None,
        observed_at: str | None,
    ) -> cli.LedgerCommandResult:
        received.update(
            automation_slug=automation_slug,
            run_id=run_id,
            step_run_id=step_run_id,
            metric_point_id=metric_point_id,
            idempotency_key=idempotency_key,
            entry_type=entry_type,
            category=category,
            amount=amount,
            currency=currency,
            source=source,
            confidence=confidence,
            observed_at=observed_at,
        )
        return cli.LedgerCommandResult(
            ledger_entry_id=41,
            automation_id=7,
            run_id=9,
            step_run_id=11,
            metric_point_id=31,
            entry_type="attributed_value",
            amount="125.5",
            currency="BRL",
            observed_at="2026-07-23T12:30:00Z",
            created=True,
        )

    monkeypatch.setattr(cli, "_record_ledger_command", record)
    exit_code = cli.main(
        [
            "record-ledger-entry",
            "--automation-slug",
            "content-engine",
            "--run-id",
            "9",
            "--step-run-id",
            "11",
            "--metric-point-id",
            "31",
            "--idempotency-key",
            private_key,
            "--type",
            "attributed_value",
            "--category",
            private_category,
            "--amount",
            "125.5",
            "--currency",
            "BRL",
            "--source",
            private_source,
            "--confidence",
            "0.75",
            "--observed-at",
            "2026-07-23T09:30:00-03:00",
            "--json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["ledger_entry_id"] == 41
    assert payload["amount"] == "125.5"
    assert received["category"] == private_category
    assert private_category not in output
    assert private_source not in output
    assert private_key not in output


def test_record_ledger_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_source = "private invoice reference"

    async def fail(
        _automation_slug: str,
        **_kwargs: object,
    ) -> cli.LedgerCommandResult:
        raise RuntimeError(f"could not read {private_source}")

    monkeypatch.setattr(cli, "_record_ledger_command", fail)
    exit_code = cli.main(
        [
            "record-ledger-entry",
            "--automation-slug",
            "content-engine",
            "--idempotency-key",
            "ledger-key",
            "--type",
            "cost",
            "--category",
            "api",
            "--amount",
            "1.25",
            "--currency",
            "USD",
            "--source",
            private_source,
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "RuntimeError" in output
    assert private_source not in output


def test_dashboard_command_starts_local_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = False

    def serve() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(cli, "_serve_dashboard_command", serve)

    assert cli.main(["dashboard"]) == 0
    assert started is True


def test_dashboard_server_uses_validated_settings(
    local_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    received: dict[str, object] = {}

    def run(app: str, **kwargs: object) -> None:
        received["app"] = app
        received.update(kwargs)

    monkeypatch.setattr(cli, "get_settings", lambda: local_settings)
    monkeypatch.setattr(uvicorn, "run", run)

    cli._serve_dashboard_command()

    assert received == {
        "app": "src.dashboard.main:app",
        "host": "127.0.0.1",
        "port": 8000,
        "reload": False,
    }


def test_supervised_dashboard_honors_cooperative_stop_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    from src.runtime import cooperative

    configuration = Settings(
        _env_file=None,
        dashboard_host="127.0.0.2",
        dashboard_port=9010,
        log_level="warning",
    )
    server = Mock(should_exit=False)
    watcher = Mock()
    config = Mock()
    monkeypatch.setenv(
        cooperative.STOP_EVENT_ENV,
        "Local\\AutomationFoundryRuntimeChildStop-123-dashboard",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: configuration)
    monkeypatch.setattr(uvicorn, "Config", Mock(return_value=config))
    monkeypatch.setattr(uvicorn, "Server", Mock(return_value=server))

    def watch(callback: object) -> Mock:
        assert callable(callback)
        callback()
        return watcher

    monkeypatch.setattr(cooperative, "start_stop_event_watcher", watch)

    cli._serve_dashboard_command()

    assert server.should_exit is True
    server.run.assert_called_once_with()
    watcher.close.assert_called_once_with()


def test_dashboard_command_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_detail = "private database location"

    def fail() -> None:
        raise RuntimeError(private_detail)

    monkeypatch.setattr(cli, "_serve_dashboard_command", fail)
    exit_code = cli.main(["dashboard"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "RuntimeError" in output
    assert private_detail not in output
