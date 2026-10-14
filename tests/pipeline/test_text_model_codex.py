"""The ``codex_exec`` text model with a fake CLI process; no quota is used."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from src.pipeline import text_model_codex, text_model_factory
from src.pipeline.text_model import (
    FallbackTextModel,
    TextModelPermanentError,
    TextModelQuotaError,
    TextModelRequest,
    TextModelRetryableError,
)
from src.pipeline.text_model_codex import (
    CODEX_DEFAULT_MODEL_ID,
    CodexExecTextModel,
    CodexRun,
    codex_prompt,
    resolve_codex_executable,
)
from src.pipeline.text_model_factory import ObservedTextModel
from src.pipeline.text_model_ollama import OllamaTextModel

_EXECUTABLE = Path("C:/tools/codex/codex.exe")
_REQUEST = TextModelRequest(
    purpose="script_hook", system="/no_think\nEscreva o hook.", user="Pauta: sono"
)
_SIGNED_IN = CodexRun(0, b"", b"Logged in using ChatGPT\n")


@dataclass
class _FakeCli:
    """Answers ``login status`` and then one ``exec`` per scripted outcome."""

    answer: str | None = "Resposta do plano."
    exec_run: CodexRun = field(default_factory=lambda: CodexRun(0, b"", b""))
    login_run: CodexRun = _SIGNED_IN
    exec_error: Exception | None = None
    commands: list[list[str]] = field(default_factory=list)
    stdin: list[bytes] = field(default_factory=list)
    environments: list[Mapping[str, str]] = field(default_factory=list)
    workspace_files: list[list[str]] = field(default_factory=list)
    schemas: list[object] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def __call__(
        self,
        command: Sequence[str],
        stdin: bytes,
        timeout_seconds: float,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CodexRun:
        self.commands.append(list(command))
        self.environments.append(env)
        self.timeouts.append(timeout_seconds)
        if list(command[1:]) == ["login", "status"]:
            return self.login_run
        self.stdin.append(stdin)
        self.workspace_files.append(sorted(path.name for path in cwd.iterdir()))
        if "--output-schema" in command:
            schema_path = Path(command[command.index("--output-schema") + 1])
            self.schemas.append(json.loads(schema_path.read_text(encoding="utf-8")))
        if self.exec_error is not None:
            raise self.exec_error
        if self.answer is not None:
            Path(command[command.index("-o") + 1]).write_text(self.answer, encoding="utf-8")
        return self.exec_run


@pytest.fixture(autouse=True)
def _native_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_model_codex, "resolve_codex_executable", lambda _name: _EXECUTABLE)


def _model(cli: _FakeCli, **kwargs: object) -> CodexExecTextModel:
    return CodexExecTextModel(runner=cli, **kwargs)  # type: ignore[arg-type]


def _events(*events: dict[str, object]) -> bytes:
    return "\n".join(json.dumps(event) for event in events).encode()


def test_runs_one_ephemeral_read_only_session_in_an_empty_folder() -> None:
    cli = _FakeCli()

    result = _model(cli).complete(_REQUEST)

    login, run = cli.commands
    assert login == [str(_EXECUTABLE), "login", "status"]
    assert run[:2] == [str(_EXECUTABLE), "exec"]
    assert run[run.index("--sandbox") + 1] == "read-only"
    for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-rules", "--json"):
        assert flag in run
    assert "-m" not in run and "--output-schema" not in run
    assert run[-1] == "-"
    assert Path(run[run.index("-C") + 1]).name == "workspace"
    assert cli.workspace_files == [[]]
    assert (result.text, result.provider, result.model_id) == (
        "Resposta do plano.",
        "codex_exec",
        CODEX_DEFAULT_MODEL_ID,
    )
    assert result.fallback_from is None


def test_the_prompt_goes_through_stdin_without_the_qwen_switch() -> None:
    cli = _FakeCli()

    _model(cli).complete(_REQUEST)

    prompt = cli.stdin[0].decode("utf-8")
    assert prompt == codex_prompt(_REQUEST)
    assert "/no_think" not in prompt
    assert "Escreva o hook." in prompt and "Pauta: sono" in prompt
    assert all("Pauta" not in argument for argument in cli.commands[1])


def test_model_and_response_schema_are_passed_to_the_cli() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    request = TextModelRequest(
        purpose="scene_plan", system="Plan.", user="Narration.", response_schema=schema
    )
    cli = _FakeCli(answer='{"a": "b"}')

    result = _model(cli, model="gpt-5.4-mini").complete(request)

    run = cli.commands[1]
    assert run[run.index("-m") + 1] == "gpt-5.4-mini"
    assert cli.schemas == [schema]
    assert result.model_id == "gpt-5.4-mini"


def test_api_keys_never_reach_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-pass")
    monkeypatch.setenv("CODEX_API_KEY", "sk-should-not-pass")
    cli = _FakeCli()

    _model(cli).complete(_REQUEST)

    for environment in cli.environments:
        assert "OPENAI_API_KEY" not in environment and "CODEX_API_KEY" not in environment


@pytest.mark.parametrize(
    "login",
    [CodexRun(1, b"", b"Not logged in\n"), CodexRun(0, b"Logged in using an API key\n", b"")],
    ids=["signed-out", "api-key"],
)
def test_without_a_chatgpt_sign_in_nothing_is_sent(login: CodexRun) -> None:
    cli = _FakeCli(login_run=login)

    with pytest.raises(TextModelPermanentError, match="ChatGPT"):
        _model(cli).complete(_REQUEST)

    assert len(cli.commands) == 1


@pytest.mark.parametrize(
    "events",
    [
        ({"type": "error", "message": "You've hit your usage limit. Try again later."},),
        (
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "429 Too Many Requests"}},
        ),
    ],
    ids=["error-event", "failed-turn"],
)
def test_a_usage_limit_is_a_quota_error(events: tuple[dict[str, object], ...]) -> None:
    cli = _FakeCli(answer=None, exec_run=CodexRun(1, _events(*events), b""))

    with pytest.raises(TextModelQuotaError):
        _model(cli).complete(_REQUEST)


def test_a_refused_sign_in_is_permanent() -> None:
    run = CodexRun(1, _events({"type": "error", "message": "401 Unauthorized"}), b"")

    with pytest.raises(TextModelPermanentError):
        _model(_FakeCli(answer=None, exec_run=run)).complete(_REQUEST)


def test_console_output_that_echoes_the_prompt_is_never_classified() -> None:
    # The topic itself may talk about rate limits; only error events count.
    run = CodexRun(1, b"not json", b"user\nComo evitar o rate limit da API?\n")

    with pytest.raises(TextModelRetryableError) as raised:
        _model(_FakeCli(answer=None, exec_run=run)).complete(_REQUEST)

    assert not isinstance(raised.value, TextModelQuotaError)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (subprocess.TimeoutExpired(cmd="codex", timeout=300), TextModelRetryableError),
        (FileNotFoundError("codex"), TextModelPermanentError),
    ],
    ids=["timeout", "cannot-start"],
)
def test_process_failures(error: Exception, expected: type[Exception]) -> None:
    with pytest.raises(expected):
        _model(_FakeCli(exec_error=error)).complete(_REQUEST)


@pytest.mark.parametrize("answer", [None, "  \n"], ids=["no-file", "blank"])
def test_an_empty_answer_is_retryable(answer: str | None) -> None:
    with pytest.raises(TextModelRetryableError, match="empty"):
        _model(_FakeCli(answer=answer)).complete(_REQUEST)


def test_the_configured_timeout_bounds_the_session() -> None:
    cli = _FakeCli()

    _model(cli, timeout_seconds=42).complete(_REQUEST)

    assert cli.timeouts[1] == 42


@pytest.mark.parametrize(
    "kwargs",
    [{"executable": " "}, {"model": "gpt; rm -rf"}, {"timeout_seconds": 5}],
    ids=["no-executable", "unsafe-model", "short-timeout"],
)
def test_configuration_is_validated(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CodexExecTextModel(**kwargs)  # type: ignore[arg-type]


def test_usage_limit_hands_the_request_to_the_local_model() -> None:
    limit = CodexRun(1, _events({"type": "error", "message": "usage_limit_reached"}), b"")
    model = FallbackTextModel(
        primary=_model(_FakeCli(answer=None, exec_run=limit)),
        fallback=OllamaTextModel(base_url="http://127.0.0.1:11434/v1", model="qwen3:14b"),
    )
    with respx.mock(assert_all_called=True) as router:
        router.post("http://127.0.0.1:11434/api/chat").mock(
            return_value=httpx.Response(200, json={"message": {"content": "Resposta local."}})
        )
        result = model.complete(_REQUEST)

    assert (result.text, result.provider, result.fallback_from) == (
        "Resposta local.",
        "ollama",
        "codex_exec",
    )


def _settings(provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434/v1",
        ollama_model="qwen3:14b",
        ollama_timeout=120,
        content_text_model_provider=provider,
        content_text_model_codex_executable="codex",
        content_text_model_codex_model="gpt-5.4-mini",
        content_text_model_codex_timeout_seconds=240,
    )


def test_ollama_is_the_configured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_model_factory, "settings", _settings("ollama"))

    model = text_model_factory.configured_text_model()

    assert isinstance(model, ObservedTextModel)
    assert isinstance(model.inner, OllamaTextModel)


def test_codex_is_configured_with_the_local_model_as_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(text_model_factory, "settings", _settings("codex_exec"))

    model = text_model_factory.configured_text_model(ollama_timeout_seconds=90)

    assert isinstance(model, ObservedTextModel)
    assert isinstance(model.inner, FallbackTextModel)
    primary, fallback = model.inner.primary, model.inner.fallback
    assert isinstance(primary, CodexExecTextModel) and isinstance(fallback, OllamaTextModel)
    assert (primary.model, primary.timeout_seconds) == ("gpt-5.4-mini", 240)
    assert (fallback.model, fallback.timeout_seconds) == ("qwen3:14b", 90)
    assert model.provider == "codex_exec"


def test_the_default_setting_is_ollama() -> None:
    from src.core.config import Settings

    assert Settings.model_fields["content_text_model_provider"].default == "ollama"


class TestExecutableResolution:
    @pytest.fixture(autouse=True)
    def _real_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            text_model_codex,
            "resolve_codex_executable",
            resolve_codex_executable,
        )

    def test_a_missing_cli_is_permanent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)

        with pytest.raises(TextModelPermanentError, match="not found"):
            resolve_codex_executable("codex")

    def test_a_native_binary_is_used_as_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary = tmp_path / "codex.exe"
        binary.write_bytes(b"")
        monkeypatch.setattr("shutil.which", lambda _name: str(binary))

        assert resolve_codex_executable("codex") == binary

    def test_the_npm_shortcut_is_replaced_by_its_native_binary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shim = tmp_path / "npm" / "codex.cmd"
        native = (
            tmp_path / "npm" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
            / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
        )
        native.parent.mkdir(parents=True)
        native.write_bytes(b"")
        shim.write_text("@echo off\n", encoding="utf-8")
        monkeypatch.setattr("shutil.which", lambda _name: str(shim))

        assert resolve_codex_executable("codex") == native

    def test_a_shortcut_without_its_native_binary_is_permanent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shim = tmp_path / "codex.cmd"
        shim.write_text("@echo off\n", encoding="utf-8")
        monkeypatch.setattr("shutil.which", lambda _name: str(shim))

        with pytest.raises(TextModelPermanentError, match="native"):
            resolve_codex_executable("codex")
