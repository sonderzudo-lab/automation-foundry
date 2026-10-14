"""The ``codex_exec`` text model: the owner's ChatGPT plan through the Codex CLI.

OpenAI documents ``codex exec`` for scripts signed in with ChatGPT. Each request
runs one non-interactive, ephemeral session:

- without a shell, from the native binary (the npm shortcut is a batch file that
  needs ``cmd.exe`` and would leave the real process running after a timeout);
- in a read-only sandbox, with an empty temporary directory as its working root,
  so the agent has no project, ``AGENTS.md`` or rules to read;
- with the prompt on stdin and the final answer read from the ``-o`` file, plus
  ``--output-schema`` when the request carries a JSON Schema;
- with ``--json`` so failures are read from the event stream, never from the
  console output, which repeats the prompt.

Before each request the CLI must report a ChatGPT sign-in, and the API key
variables are removed from its environment: an API key would bill the account,
and the plan exists precisely so the pipeline spends nothing. Prompt and answer
are never logged. A usage-limit message becomes ``TextModelQuotaError``, which
``FallbackTextModel`` hands to the local model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from src.pipeline.text_model import (
    TextModelPermanentError,
    TextModelProviderName,
    TextModelQuotaError,
    TextModelRequest,
    TextModelResult,
    TextModelRetryableError,
)

CODEX_EXEC_TIMEOUT_SECONDS = 300.0
CODEX_LOGIN_TIMEOUT_SECONDS = 30.0
# Reported as the model when none is configured and the CLI chooses its own.
CODEX_DEFAULT_MODEL_ID = "codex-default"

# Variables through which the CLI would use a paid API key instead of the plan.
_API_KEY_VARIABLES = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"})
_SHIM_SUFFIXES = frozenset({".cmd", ".bat", ".ps1", ".js"})
_NATIVE_BINARY_GLOBS = (
    "node_modules/@openai/codex-*/vendor/*/bin/codex.exe",
    "node_modules/@openai/codex-*/vendor/*/bin/codex",
)
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
# Matched only against error events, never against text that may echo the prompt.
# Still to be confirmed against a real limit; see docs/reference/content-contracts-v1.md.
_QUOTA_MESSAGE = re.compile(
    r"usage[ _]limit|rate[ _]limit|too many requests|quota|\b429\b", re.IGNORECASE
)
_SIGN_IN_MESSAGE = re.compile(
    r"not logged in|logged out|\blog ?in\b|\bsign ?in\b|unauthori[sz]ed|\b401\b|\b403\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CodexRun:
    """What one CLI process returned."""

    returncode: int
    stdout: bytes
    stderr: bytes


# (command, stdin, timeout, working directory, environment) -> finished process.
CodexRunner = Callable[[Sequence[str], bytes, float, Path, Mapping[str, str]], CodexRun]


def run_codex_process(
    command: Sequence[str],
    stdin: bytes,
    timeout_seconds: float,
    cwd: Path,
    env: Mapping[str, str],
) -> CodexRun:
    """Run the CLI once, without a shell; ``subprocess.TimeoutExpired`` propagates."""
    completed = subprocess.run(
        list(command),
        input=stdin,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
        shell=False,
        cwd=cwd,
        env=dict(env),
    )
    return CodexRun(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class CodexExecTextModel:
    """The ChatGPT plan, one ephemeral ``codex exec`` session per request."""

    executable: str = "codex"
    model: str | None = None
    timeout_seconds: float = CODEX_EXEC_TIMEOUT_SECONDS
    runner: CodexRunner = field(default=run_codex_process, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.executable.strip():
            raise ValueError("a Codex executable is required")
        if self.model is not None and not _MODEL_NAME.fullmatch(self.model):
            raise ValueError("the Codex model name is invalid")
        if not 10 <= self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds is out of range")

    @property
    def provider(self) -> TextModelProviderName:
        return "codex_exec"

    @property
    def model_id(self) -> str:
        return self.model or CODEX_DEFAULT_MODEL_ID

    def complete(self, request: TextModelRequest) -> TextModelResult:
        executable = resolve_codex_executable(self.executable)
        environment = {
            name: value
            for name, value in os.environ.items()
            if name.upper() not in _API_KEY_VARIABLES
        }
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="codex-text-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            self._require_chatgpt_sign_in(executable, workspace, environment)
            answer_file = root / "answer.txt"
            command = [
                str(executable),
                "exec",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-rules",
                "--color", "never",
                "--json",
                "-C", str(workspace),
            ]
            if self.model is not None:
                command += ["-m", self.model]
            if request.response_schema is not None:
                schema_file = root / "schema.json"
                schema_file.write_text(json.dumps(dict(request.response_schema)), encoding="utf-8")
                command += ["--output-schema", str(schema_file)]
            command += ["-o", str(answer_file), "-"]
            try:
                run = self.runner(
                    command,
                    codex_prompt(request).encode("utf-8"),
                    self.timeout_seconds,
                    workspace,
                    environment,
                )
            except subprocess.TimeoutExpired as exc:
                raise TextModelRetryableError("codex exec timed out") from exc
            except OSError as exc:
                raise TextModelPermanentError("codex exec could not be started") from exc
            if run.returncode != 0:
                raise _failure(run)
            answer = _read_answer(answer_file)
        if not answer:
            raise TextModelRetryableError("codex exec returned an empty answer")
        return TextModelResult(
            text=answer,
            provider="codex_exec",
            model_id=self.model_id,
            duration_seconds=Decimal(f"{time.monotonic() - started:.3f}"),
        )

    def _require_chatgpt_sign_in(
        self, executable: Path, workspace: Path, environment: Mapping[str, str]
    ) -> None:
        try:
            status = self.runner(
                [str(executable), "login", "status"],
                b"",
                CODEX_LOGIN_TIMEOUT_SECONDS,
                workspace,
                environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TextModelRetryableError("codex login status did not answer") from exc
        output = (status.stdout + status.stderr).decode("utf-8", errors="replace")
        if status.returncode != 0 or "chatgpt" not in output.lower():
            raise TextModelPermanentError("codex is not signed in with a ChatGPT plan")


def codex_prompt(request: TextModelRequest) -> str:
    """One prompt for the agent: the system rules, then the request, answer only."""
    # "/no_think" is a Qwen3 switch; it means nothing to the Codex models.
    system = "\n".join(
        line for line in request.system.splitlines() if line.strip() != "/no_think"
    ).strip()
    return (
        "You are used as a plain text model. Do not run commands, read or write files or "
        "use any tool. Follow the instructions and answer the request with the final "
        "answer only.\n\n"
        f"<instructions>\n{system}\n</instructions>\n\n"
        f"<request>\n{request.user.strip()}\n</request>\n"
    )


def resolve_codex_executable(configured: str) -> Path:
    """Find the native Codex binary, looking past the npm shortcut if needed."""
    found = shutil.which(configured)
    if found is None:
        raise TextModelPermanentError("codex CLI was not found")
    path = Path(found)
    if path.suffix.lower() not in _SHIM_SUFFIXES:
        return path
    resolved = path.resolve()
    package_roots = {
        path.parent / "node_modules" / "@openai" / "codex",
        resolved.parent.parent,
    }
    candidates = sorted(
        {
            candidate
            for package_root in package_roots
            for pattern in _NATIVE_BINARY_GLOBS
            for candidate in package_root.glob(pattern)
            if candidate.is_file()
        }
    )
    if len(candidates) != 1:
        raise TextModelPermanentError("the native codex binary behind the npm shortcut was not found")
    return candidates[0]


def _failure(run: CodexRun) -> Exception:
    messages = " | ".join(_event_errors(run.stdout))
    if _QUOTA_MESSAGE.search(messages):
        return TextModelQuotaError("codex usage limit reached")
    if _SIGN_IN_MESSAGE.search(messages):
        return TextModelPermanentError("codex refused the sign-in")
    return TextModelRetryableError(f"codex exec failed (exit {run.returncode})")


def _event_errors(stdout: bytes) -> list[str]:
    """Error messages from the ``--json`` event stream."""
    messages: list[str] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            messages.append(event["message"])
        error = event.get("error")
        if event.get("type") == "turn.failed" and isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                messages.append(message)
    return messages


def _read_answer(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise TextModelRetryableError("codex answer could not be read") from exc
