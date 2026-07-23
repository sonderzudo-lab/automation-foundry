"""Local, side-effect-free command line interface for Automation Foundry."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import socket
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from importlib import metadata
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from src.core.config import Settings, get_settings

if TYPE_CHECKING:
    from src.platform.example_run import ExampleRunResult

CheckStatus = Literal["pass", "fail", "skip"]
_REDIS_SCHEMES = frozenset({"redis", "rediss"})
_OLLAMA_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One redacted diagnostic result suitable for text or JSON output."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ControlCommandResult:
    """Redacted result of one explicit local operator control command."""

    action: str
    target_id: int
    changed: bool


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _network_endpoint(url: str, default_port: int) -> tuple[str, int] | None:
    parsed = urlsplit(url)
    if parsed.hostname is None:
        return None
    try:
        port = parsed.port or default_port
    except ValueError:
        return None
    return parsed.hostname, port


def _check_python() -> CheckResult:
    version = sys.version_info
    detail = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 12):
        return CheckResult("python", "pass", detail)
    return CheckResult("python", "fail", f"{detail}; requer Python 3.12+")


def _check_package() -> CheckResult:
    try:
        version = metadata.version("automation-foundry")
    except metadata.PackageNotFoundError:
        return CheckResult("package", "fail", "automation-foundry não está instalado")
    return CheckResult("package", "pass", f"automation-foundry {version}")


def _check_database(settings: Settings) -> CheckResult:
    url = settings.database_url
    if url.startswith("sqlite+aiosqlite:///"):
        return CheckResult("database", "pass", "SQLite local (processo único)")

    if url.startswith("postgresql+"):
        endpoint = _network_endpoint(url, 5432)
        if endpoint is not None and _is_loopback_host(endpoint[0]):
            return CheckResult("database", "pass", "PostgreSQL em loopback")
        return CheckResult("database", "fail", "PostgreSQL deve usar loopback")

    return CheckResult("database", "fail", "driver de banco não suportado")


def _check_loopback_url(
    name: str,
    url: str,
    default_port: int,
    allowed_schemes: frozenset[str],
) -> CheckResult:
    if urlsplit(url).scheme.casefold() not in allowed_schemes:
        return CheckResult(name, "fail", "protocolo não suportado")
    endpoint = _network_endpoint(url, default_port)
    if endpoint is None:
        return CheckResult(name, "fail", "endpoint inválido")
    if not _is_loopback_host(endpoint[0]):
        return CheckResult(name, "fail", "endpoint deve usar loopback")
    return CheckResult(name, "pass", f"loopback:{endpoint[1]}")


def _check_tcp_service(
    name: str,
    url: str,
    default_port: int,
    allowed_schemes: frozenset[str],
) -> CheckResult:
    if urlsplit(url).scheme.casefold() not in allowed_schemes:
        return CheckResult(name, "fail", "protocolo não suportado; sonda não executada")
    endpoint = _network_endpoint(url, default_port)
    if endpoint is None:
        return CheckResult(name, "fail", "endpoint inválido; sonda não executada")
    if not _is_loopback_host(endpoint[0]):
        return CheckResult(name, "fail", "endpoint fora de loopback; sonda não executada")

    try:
        connection = socket.create_connection(endpoint, timeout=0.5)
    except OSError:
        return CheckResult(name, "fail", "indisponível em loopback")
    connection.close()
    return CheckResult(name, "pass", "porta local acessível")


def run_doctor(settings: Settings, *, check_services: bool = False) -> list[CheckResult]:
    """Run redacted local checks without mutating application or service state."""
    results = [
        _check_python(),
        _check_package(),
        _check_database(settings),
        _check_loopback_url(
            "redis_config",
            settings.redis_url,
            6379,
            _REDIS_SCHEMES,
        ),
        _check_loopback_url(
            "ollama_config",
            settings.ollama_base_url,
            11434,
            _OLLAMA_SCHEMES,
        ),
    ]

    if check_services:
        results.extend(
            [
                _check_tcp_service(
                    "redis_service",
                    settings.redis_url,
                    6379,
                    _REDIS_SCHEMES,
                ),
                _check_tcp_service(
                    "ollama_service",
                    settings.ollama_base_url,
                    11434,
                    _OLLAMA_SCHEMES,
                ),
            ]
        )
    else:
        results.extend(
            [
                CheckResult("redis_service", "skip", "use --services para verificar"),
                CheckResult("ollama_service", "skip", "use --services para verificar"),
            ]
        )

    return results


def _summary(results: Sequence[CheckResult]) -> dict[str, int]:
    return {
        status: sum(result.status == status for result in results)
        for status in ("pass", "fail", "skip")
    }


def _render_text(results: Sequence[CheckResult]) -> None:
    print("Automation Foundry doctor")
    for result in results:
        print(f"[{result.status.upper():4}] {result.name}: {result.detail}")
    counts = _summary(results)
    outcome = "PASS" if counts["fail"] == 0 else "FAIL"
    print(
        f"Resultado: {outcome} "
        f"({counts['pass']} pass, {counts['fail']} fail, {counts['skip']} skip)"
    )


def _render_json(results: Sequence[CheckResult]) -> None:
    counts = _summary(results)
    payload = {
        "ok": counts["fail"] == 0,
        "summary": counts,
        "checks": [asdict(result) for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automation-foundry",
        description="Operação local segura do Automation Foundry.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser(
        "doctor",
        help="Valida o baseline local sem alterar estado.",
    )
    doctor.add_argument(
        "--services",
        action="store_true",
        help="Testa portas locais de Redis e Ollama com timeout curto.",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Emite resultados estruturados em JSON.",
    )
    run_example = subparsers.add_parser(
        "run-example",
        help="Executa um passo no-op local e persiste seu ciclo de vida.",
    )
    run_example.add_argument(
        "--idempotency-key",
        required=True,
        help="Chave estável para impedir execuções duplicadas.",
    )
    run_example.add_argument(
        "--json",
        action="store_true",
        help="Emite o resultado estruturado em JSON.",
    )
    kill_switch = subparsers.add_parser(
        "kill-switch",
        help="Ativa ou desativa o kill switch de uma automação local.",
    )
    kill_switch.add_argument("--automation-slug", required=True)
    kill_switch_state = kill_switch.add_mutually_exclusive_group(required=True)
    kill_switch_state.add_argument("--enable", action="store_true")
    kill_switch_state.add_argument("--disable", action="store_true")
    kill_switch.add_argument("--reason", required=True)
    kill_switch.add_argument("--json", action="store_true")

    cancel_run = subparsers.add_parser(
        "cancel-run",
        help="Solicita o cancelamento seguro de uma run local.",
    )
    cancel_run.add_argument("--run-id", required=True, type=int)
    cancel_run.add_argument("--reason", required=True)
    cancel_run.add_argument("--json", action="store_true")
    return parser


async def _execute_example_command(idempotency_key: str) -> ExampleRunResult:
    from src.core.database import AsyncSessionLocal
    from src.platform.example_run import execute_example_run

    async with AsyncSessionLocal() as session:
        try:
            result = await execute_example_run(
                session,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return result


def _render_example_result(result: ExampleRunResult, *, as_json: bool) -> None:
    payload = asdict(result)
    if as_json:
        print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))
        return
    replay = " (resultado idempotente existente)" if payload["replayed"] else ""
    print(
        f"Run {payload['run_id']} {payload['run_status']}; "
        f"step {payload['step_run_id']} {payload['step_status']}{replay}"
    )


async def _set_kill_switch_command(
    automation_slug: str,
    *,
    active: bool,
    reason: str,
) -> ControlCommandResult:
    from sqlalchemy import select

    from src.core.database import AsyncSessionLocal
    from src.platform.control_service import set_automation_kill_switch
    from src.platform.models import Automation

    async with AsyncSessionLocal() as session:
        try:
            automation = await session.scalar(
                select(Automation).where(Automation.slug == automation_slug.strip())
            )
            if automation is None:
                raise ValueError("automation does not exist")
            change = await set_automation_kill_switch(
                session,
                automation=automation,
                active=active,
                reason=reason,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return ControlCommandResult(
        action="kill_switch_enabled" if active else "kill_switch_disabled",
        target_id=automation.id,
        changed=change.changed,
    )


async def _cancel_run_command(run_id: int, *, reason: str) -> ControlCommandResult:
    from src.core.database import AsyncSessionLocal
    from src.platform.control_service import request_run_cancellation
    from src.platform.models import Run

    async with AsyncSessionLocal() as session:
        try:
            run = await session.get(Run, run_id)
            if run is None:
                raise ValueError("run does not exist")
            change = await request_run_cancellation(
                session,
                run=run,
                reason=reason,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return ControlCommandResult(
        action="cancellation_requested",
        target_id=run.id,
        changed=change.changed,
    )


def _render_control_result(result: ControlCommandResult, *, as_json: bool) -> None:
    payload = asdict(result)
    if as_json:
        print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))
        return
    outcome = "alterado" if result.changed else "sem alteração (idempotente)"
    print(f"{result.action} no alvo {result.target_id}: {outcome}")


def _render_command_failure(command: str, exc: Exception, *, as_json: bool) -> int:
    detail = f"{command} falhou ({type(exc).__name__})"
    if as_json:
        print(json.dumps({"ok": False, "error": detail}, ensure_ascii=False))
    else:
        print(detail)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Automation Foundry CLI and return a process exit code."""
    args = _build_parser().parse_args(argv)

    if args.command == "doctor":
        try:
            settings = get_settings()
        except Exception as exc:
            results = [
                CheckResult(
                    "configuration",
                    "fail",
                    f"configuração inválida ({type(exc).__name__})",
                )
            ]
        else:
            results = run_doctor(settings, check_services=bool(args.services))

        if args.json:
            _render_json(results)
        else:
            _render_text(results)
        return int(any(result.status == "fail" for result in results))

    if args.command == "run-example":
        try:
            example_result = asyncio.run(
                _execute_example_command(args.idempotency_key)
            )
        except Exception as exc:
            detail = f"run-example falhou ({type(exc).__name__})"
            if args.json:
                print(json.dumps({"ok": False, "error": detail}, ensure_ascii=False))
            else:
                print(detail)
            return 1
        _render_example_result(example_result, as_json=bool(args.json))
        return 0

    if args.command == "kill-switch":
        try:
            control_result = asyncio.run(
                _set_kill_switch_command(
                    args.automation_slug,
                    active=bool(args.enable),
                    reason=args.reason,
                )
            )
        except Exception as exc:
            return _render_command_failure("kill-switch", exc, as_json=bool(args.json))
        _render_control_result(control_result, as_json=bool(args.json))
        return 0

    if args.command == "cancel-run":
        try:
            cancellation_result = asyncio.run(
                _cancel_run_command(
                    args.run_id,
                    reason=args.reason,
                )
            )
        except Exception as exc:
            return _render_command_failure("cancel-run", exc, as_json=bool(args.json))
        _render_control_result(cancellation_result, as_json=bool(args.json))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
