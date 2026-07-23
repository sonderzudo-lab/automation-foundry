"""Local, side-effect-free command line interface for Automation Foundry."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from importlib import metadata
from typing import Literal
from urllib.parse import urlsplit

from src.core.config import Settings, get_settings

CheckStatus = Literal["pass", "fail", "skip"]
_REDIS_SCHEMES = frozenset({"redis", "rediss"})
_OLLAMA_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One redacted diagnostic result suitable for text or JSON output."""

    name: str
    status: CheckStatus
    detail: str


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
    return parser


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

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
