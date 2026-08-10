"""Safe Windows startup, shutdown, and status commands for the local runtime."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from src.core.config import get_settings
from src.runtime.supervisor import (
    RuntimeConfigurationError,
    load_runtime_status,
    run_supervisor,
)
from src.runtime.windows import (
    RuntimeAlreadyRunningError,
    signal_runtime_stop,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="automation-foundry-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="Inicia e supervisiona o runtime em foreground.")
    subparsers.add_parser("stop", help="Solicita shutdown gracioso ao supervisor ativo.")
    status = subparsers.add_parser("status", help="Mostra somente estados redigidos.")
    status.add_argument("--json", action="store_true", help="Emite JSON estruturado.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Operate the fixed local runtime without accepting command overrides."""
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "stop":
        if signal_runtime_stop():
            print("Runtime shutdown requested.")
            return 0
        print("Runtime is not active.", file=sys.stderr)
        return 1
    try:
        configuration = get_settings()
    except Exception:
        print("Runtime configuration could not be loaded.", file=sys.stderr)
        return 2
    if args.command == "start":
        try:
            run_supervisor(configuration)
        except (RuntimeAlreadyRunningError, RuntimeConfigurationError) as exc:
            print(f"Runtime configuration error: {exc}", file=sys.stderr)
            return 2
        except Exception:
            print("Runtime failed; inspect local runtime logs.", file=sys.stderr)
            return 1
        return 0

    status = load_runtime_status(configuration)
    payload = {"running": status.running, "services": status.services}
    if bool(args.json):
        print(json.dumps(payload, sort_keys=True))
    else:
        print("runtime: " + ("running" if status.running else "stopped"))
        for name, service_status in sorted(status.services.items()):
            print(f"{name}: {service_status}")
    return 0 if status.running else 1


if __name__ == "__main__":
    raise SystemExit(main())
