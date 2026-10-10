"""Operator-requested, read-only readiness diagnostic for the Content Engine.

The diagnostic is advisory: it never feeds dispatch, approvals or step gates, and it
does not replace the full verification each step runs (hashes, media inspection,
licensing evidence). Every check is isolated: a timeout, exception or malformed answer
turns into a closed ``ProbeCode`` for that check only. No raw exception, endpoint or
filesystem path is ever placed in the report.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from urllib.parse import urlsplit

import httpx
import structlog

from src.core.config import Settings, get_settings
from src.pipeline import readiness_checks as checks
from src.pipeline.assembly import FFMPEG_REQUIRED_BUILD_FLAGS
from src.pipeline.captions import (
    CTRANSLATE2_PACKAGE_VERSION,
    FASTER_WHISPER_PACKAGE_VERSION,
    FASTER_WHISPER_QUALITY_TEST_BACKEND,
    caption_snapshot_file_sizes,
)
from src.pipeline.readiness_checks import ProbeCode, ProbeOutcome
from src.pipeline.tts import (
    KOKORO_MODEL_REPOSITORY,
    KOKORO_MODEL_REVISION,
    KOKORO_PACKAGE_VERSION,
    KOKORO_PT_BR_VOICE_SHA256_PREFIXES,
    KOKORO_QUALITY_TEST_BACKEND,
)

log = structlog.get_logger(__name__)

LOCAL_DEADLINE_SECONDS = 6.0
OLLAMA_DEADLINE_SECONDS = 3.0
OLLAMA_HTTP_TIMEOUT_SECONDS = 2.0
_OLLAMA_BODY_LIMIT_BYTES = 1024 * 1024
# Dedicated and bounded so a hung filesystem call can never exhaust the loop's
# default executor; an abandoned thread is reported as TIMEOUT, not waited for.
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="content-readiness")


class ReadinessStatus(StrEnum):
    READY = "ready"
    PROBLEM = "problem"
    NOT_APPLICABLE = "not_applicable"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    id: str
    label: str
    status: ReadinessStatus
    code: ProbeCode
    guidance: str
    checked_at: datetime
    free_bytes: int | None = None

    @property
    def result_code(self) -> str:
        """Stable, operator-visible identifier, e.g. ``CONTENT_READINESS_OLLAMA_MISSING``."""
        return f"CONTENT_READINESS_{self.id.upper()}_{self.code.value}"


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    started_at: datetime
    completed_at: datetime
    checks: tuple[ReadinessCheck, ...]

    @property
    def counts(self) -> dict[ReadinessStatus, int]:
        return {
            status: sum(1 for check in self.checks if check.status is status)
            for status in ReadinessStatus
        }


@dataclass(frozen=True, slots=True)
class _Spec:
    id: str
    label: str
    messages: Mapping[ProbeCode, str]


_GENERIC_MESSAGES: Mapping[ProbeCode, str] = {
    ProbeCode.TIMEOUT: (
        "A consulta excedeu o prazo e foi interrompida. Confira se a dependência local "
        "responde e consulte novamente."
    ),
    ProbeCode.NOT_CHECKED: (
        "Não foi possível concluir esta consulta. Confira a instalação local e consulte "
        "novamente."
    ),
}
_UNAVAILABLE_FILES = (
    "Não foi possível ler o recurso local. Confira permissões e se o disco está disponível."
)


def _media_messages(tool: str, path_setting: str, hash_setting: str) -> dict[ProbeCode, str]:
    flags = "GPL, version3, static, libx264 e libass"
    requirement = f" com o build exigido ({flags})" if tool == "ffmpeg" else ""
    return {
        ProbeCode.OK: (
            f"{tool} encontrado e respondeu ao -version{requirement}. O formato do SHA-256 "
            "configurado é válido; o hash do arquivo e a mídia só são conferidos no step."
        ),
        ProbeCode.INVALID_CONFIG: (
            f"{path_setting} está vazio ou {hash_setting} está ausente ou inválido. Informe o "
            "executável e um digest SHA-256 de 64 caracteres hexadecimais."
        ),
        ProbeCode.MISSING: (
            f"Executável não encontrado. Confira {path_setting} e o PATH do runtime."
        ),
        ProbeCode.INCOMPATIBLE: (
            f"O executável respondeu, mas não é um {tool}{requirement}. Use o build fixado."
        ),
        ProbeCode.UNAVAILABLE: (
            f"Não foi possível executar {tool}. Confira permissões e antivírus."
        ),
    }


_OLLAMA = _Spec("ollama", "Roteiro · Ollama e modelo configurado", {
    ProbeCode.OK: (
        "O Ollama respondeu e o modelo configurado está na lista. A consulta apenas lista "
        "modelos: não testa inferência nem carrega o modelo na GPU."
    ),
    ProbeCode.INVALID_CONFIG: (
        "OLLAMA_BASE_URL ou OLLAMA_MODEL está inválido. Use um endereço http(s) em loopback, "
        "sem credenciais, e informe o nome do modelo."
    ),
    ProbeCode.UNAVAILABLE: (
        "O Ollama não respondeu. Inicie o aplicativo ou o serviço do Ollama e confira "
        "OLLAMA_BASE_URL."
    ),
    ProbeCode.MISSING: (
        "O Ollama respondeu, mas o modelo configurado não está na lista. Baixe ou importe o "
        "modelo no Ollama ou corrija OLLAMA_MODEL."
    ),
    ProbeCode.INCOMPATIBLE: (
        "O Ollama respondeu com erro ou em formato inesperado. Atualize o Ollama e confira "
        "se OLLAMA_BASE_URL termina em /v1."
    ),
})
_STORAGE = _Spec("storage", "Armazenamento · STORAGE_ROOT", {
    ProbeCode.OK: (
        "A pasta existe, pode ser lida e tem espaço livre acima do piso de alerta de 1 GiB. "
        "A gravação real não é testada."
    ),
    ProbeCode.LOW_SPACE: (
        "Há menos de 1 GiB livre (piso de alerta, não uma estimativa do tamanho de um vídeo). "
        "Libere espaço ou aponte STORAGE_ROOT para outro disco antes de executar."
    ),
    ProbeCode.MISSING: (
        "A pasta de STORAGE_ROOT não existe. Crie-a, reconecte o disco ou corrija "
        "STORAGE_ROOT no .env."
    ),
    ProbeCode.INVALID_CONFIG: (
        "STORAGE_ROOT está vazio ou aponta para um arquivo. Informe o caminho de uma pasta."
    ),
    ProbeCode.UNAVAILABLE: (
        "Sem acesso à pasta de storage. Confira permissões e se o disco está disponível."
    ),
})
_TTS_PACKAGES = _Spec("tts_packages", "Narração · pacotes do Kokoro", {
    ProbeCode.OK: (
        "Kokoro na versão fixada e os módulos exigidos (torch, numpy, huggingface_hub) estão "
        "instalados. Nada foi importado nem carregado."
    ),
    ProbeCode.MISSING: (
        "Há pacote ou módulo ausente no ambiente do runtime. Reinstale as dependências de "
        "narração (Kokoro, PyTorch, NumPy e huggingface_hub)."
    ),
    ProbeCode.INCOMPATIBLE: (
        "A versão instalada do Kokoro difere da fixada pelo adapter. Instale a versão exigida."
    ),
})
_TTS_ESPEAK = _Spec("tts_espeak", "Narração · eSpeak NG", {
    ProbeCode.OK: "O espeak-ng foi encontrado no PATH do runtime.",
    ProbeCode.MISSING: (
        "O espeak-ng não está no PATH. Instale o eSpeak NG e reinicie o runtime para que o "
        "PATH seja relido."
    ),
})
_TTS_CACHE = _Spec("tts_cache", "Narração · cache do modelo e da voz", {
    ProbeCode.OK: (
        "Config, pesos e voz fixados já estão no cache local. Nenhum arquivo foi baixado nem "
        "recebeu hash completo."
    ),
    ProbeCode.DEFERRED: (
        "Os arquivos fixados ainda não estão no cache local. A primeira narração os baixa do "
        "Hugging Face (exige rede e espaço); isso não impede iniciar o roteiro. Prepare o "
        "cache antes para evitar o download durante a execução."
    ),
    ProbeCode.MISSING: (
        "O cache está incompleto e HF_HUB_OFFLINE está ativo, então o step não poderá "
        "baixá-lo. Prepare o cache ou desative o modo offline."
    ),
    ProbeCode.INVALID_CONFIG: (
        "CONTENT_TTS_VOICE_ID não é uma voz revisada pelo projeto. Use uma voz PT-BR permitida."
    ),
    ProbeCode.UNAVAILABLE: _UNAVAILABLE_FILES,
})
_CAPTION_PACKAGES = _Spec("caption_packages", "Legendas · pacotes de transcrição", {
    ProbeCode.OK: (
        "faster-whisper e CTranslate2 estão instalados nas versões fixadas. Nada foi "
        "importado nem carregado na GPU."
    ),
    ProbeCode.MISSING: (
        "faster-whisper ou CTranslate2 não está instalado no ambiente do runtime. Instale as "
        "versões fixadas pelo adapter."
    ),
    ProbeCode.INCOMPATIBLE: (
        "Uma versão instalada difere da fixada pelo adapter. Instale as versões exigidas."
    ),
})
_CAPTION_MODEL = _Spec("caption_model", "Legendas · arquivos do modelo faster-whisper", {
    ProbeCode.OK: (
        "O snapshot está presente, legível e com os tamanhos esperados. Os hashes só são "
        "conferidos pelo step; nenhum modelo foi carregado."
    ),
    ProbeCode.MISSING: (
        "O snapshot não foi encontrado em CONTENT_CAPTION_MODEL_PATH. Copie para lá a revisão "
        "fixada do modelo; o runtime não baixa o modelo."
    ),
    ProbeCode.INCOMPLETE: (
        "O snapshot está incompleto ou tem arquivo de tamanho divergente. Refaça a cópia da "
        "revisão fixada do modelo."
    ),
    ProbeCode.INVALID_CONFIG: (
        "CONTENT_CAPTION_MODEL_PATH está vazio ou aponta para um arquivo. Informe a pasta do "
        "snapshot."
    ),
    ProbeCode.UNAVAILABLE: _UNAVAILABLE_FILES,
})
_FFMPEG = _Spec("ffmpeg", "Montagem · FFmpeg", _media_messages(
    "ffmpeg", "CONTENT_FFMPEG_PATH", "CONTENT_FFMPEG_EXPECTED_SHA256",
))
_FFPROBE = _Spec("ffprobe", "Montagem · ffprobe", _media_messages(
    "ffprobe", "CONTENT_FFPROBE_PATH", "CONTENT_FFPROBE_EXPECTED_SHA256",
))
_VISUAL_CONFIG = _Spec("visual_config", "Visuais · configuração da importação", {
    ProbeCode.OK: (
        "A pasta de importação e o manifesto estão configurados de forma coerente e contida. "
        "Manifesto e imagens são insumos da run, preparados depois."
    ),
    ProbeCode.INVALID_CONFIG: (
        "CONTENT_VISUAL_IMPORT_ROOT e CONTENT_VISUAL_MANIFEST_PATH precisam apontar para uma "
        "pasta própria (nunca a raiz do disco) com o manifesto dentro dela."
    ),
    ProbeCode.UNAVAILABLE: _UNAVAILABLE_FILES,
})
_VISUAL_INPUTS = _Spec("visual_inputs", "Visuais · manifesto e imagens da run", {
    ProbeCode.DEFERRED: (
        "Insumo posterior: a quantidade e as dimensões das imagens dependem da duração da "
        "narração, que ainda não existe. Prepare manifesto, imagens, hashes e direitos "
        "depois do A2; o step confere tudo. Isto não impede iniciar o roteiro."
    ),
})
_GPU_RUNTIME = _Spec("gpu_runtime", "GPU · CUDA e carga dos modelos", {
    ProbeCode.DEFERRED: (
        "Não verificado por este diagnóstico, que não carrega modelos na GPU. CUDA, VRAM e "
        "carga real são confirmadas pelo próprio step na execução."
    ),
})
_PIPELINE = _Spec("pipeline_config", "Etapas · coerência da configuração", {
    ProbeCode.OK: "As etapas habilitadas formam uma cadeia coerente.",
})

_RESULT_STATUS: Mapping[ProbeCode, ReadinessStatus] = {
    ProbeCode.OK: ReadinessStatus.READY,
    ProbeCode.DISABLED: ReadinessStatus.NOT_APPLICABLE,
    ProbeCode.NOT_CHECKED: ReadinessStatus.NOT_CHECKED,
    ProbeCode.DEFERRED: ReadinessStatus.NOT_CHECKED,
    ProbeCode.TIMEOUT: ReadinessStatus.NOT_CHECKED,
}


def _build(
    spec: _Spec,
    outcome: ProbeOutcome,
    *,
    not_applicable_reason: str = "",
) -> ReadinessCheck:
    code = outcome.code
    status = _RESULT_STATUS.get(code, ReadinessStatus.PROBLEM)
    if code is ProbeCode.DISABLED:
        guidance = not_applicable_reason
    else:
        guidance = (
            spec.messages.get(code)
            or _GENERIC_MESSAGES.get(code)
            or _GENERIC_MESSAGES[ProbeCode.NOT_CHECKED]
        )
    free = outcome.free_bytes
    if isinstance(free, bool) or not isinstance(free, int) or free < 0:
        free = None
    return ReadinessCheck(spec.id, spec.label, status, code, guidance, datetime.now(UTC), free)


def _not_applicable(spec: _Spec, reason: str) -> ReadinessCheck:
    return _build(spec, ProbeOutcome(ProbeCode.DISABLED), not_applicable_reason=reason)


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _probe_ollama(
    configuration: Settings,
    transport: httpx.AsyncBaseTransport | None,
) -> ProbeOutcome:
    """List the models of the same OpenAI-compatible root A1 uses; no inference."""
    base = configuration.ollama_base_url.strip().rstrip("/")
    model = configuration.ollama_model.strip()
    parsed = urlsplit(base)
    try:
        host = parsed.hostname
        parsed.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return ProbeOutcome(ProbeCode.INVALID_CONFIG)
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not model
        or not _is_loopback(host)
    ):
        return ProbeOutcome(ProbeCode.INVALID_CONFIG)
    try:
        async with httpx.AsyncClient(
            timeout=OLLAMA_HTTP_TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        ) as client, client.stream("GET", f"{base}/models") as response:
            if response.status_code != 200:
                return ProbeOutcome(ProbeCode.INCOMPATIBLE)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _OLLAMA_BODY_LIMIT_BYTES:
                    return ProbeOutcome(ProbeCode.INCOMPATIBLE)
    except httpx.TimeoutException:
        return ProbeOutcome(ProbeCode.TIMEOUT)
    except httpx.HTTPError:
        return ProbeOutcome(ProbeCode.UNAVAILABLE)
    try:
        payload = json.loads(bytes(body))
    except ValueError:
        return ProbeOutcome(ProbeCode.INCOMPATIBLE)
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return ProbeOutcome(ProbeCode.INCOMPATIBLE)
    names = {
        item["id"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    # Ollama resolves an untagged name to ``:latest``.
    listed = model in names or (":" not in model and f"{model}:latest" in names)
    return ProbeOutcome(ProbeCode.OK if listed else ProbeCode.MISSING)


def _pipeline_conflicts(configuration: Settings) -> tuple[str, ...]:
    """Static texts for each enabled-stage dependency the executor would refuse."""
    tts = configuration.content_tts_backend != "disabled"
    visual = configuration.content_visual_backend != "disabled"
    caption = configuration.content_caption_backend != "disabled"
    assembly = configuration.content_assembly_backend != "disabled"
    narration_review = configuration.content_narration_review_enabled
    final_review = configuration.content_final_review_enabled
    conflicts: list[str] = []
    if (visual or caption or assembly or narration_review) and not tts:
        conflicts.append(
            "Visuais, legendas, montagem e revisão da narração exigem a narração habilitada."
        )
    if narration_review and not visual:
        conflicts.append("A revisão da narração exige os visuais habilitados.")
    if assembly and not (visual and caption):
        conflicts.append("A montagem exige visuais e legendas habilitados.")
    if configuration.content_thumbnail_review_enabled and not final_review:
        conflicts.append("A revisão da thumbnail exige a revisão final do vídeo.")
    if configuration.content_caption_alignment_gate_enabled:
        if not final_review:
            conflicts.append("O gate de alinhamento exige a revisão final do vídeo.")
        if not caption:
            conflicts.append("O gate de alinhamento exige as legendas habilitadas.")
        if not assembly:
            conflicts.append("O gate de alinhamento exige a montagem habilitada.")
    return tuple(conflicts)


async def _guarded(
    spec: _Spec,
    probe: Callable[[], ProbeOutcome],
    *,
    deadline_seconds: float,
) -> ReadinessCheck:
    """Run one synchronous probe in the bounded pool; never raise."""
    try:
        loop = asyncio.get_running_loop()
        outcome = await asyncio.wait_for(
            loop.run_in_executor(_PROBE_EXECUTOR, probe), timeout=deadline_seconds,
        )
    except TimeoutError:
        outcome = ProbeOutcome(ProbeCode.TIMEOUT)
    except Exception as exc:
        log.warning("content_readiness_probe_failed", check=spec.id, error=type(exc).__name__)
        outcome = ProbeOutcome(ProbeCode.NOT_CHECKED)
    return _build(spec, _coerce(outcome))


def _coerce(outcome: object) -> ProbeOutcome:
    if isinstance(outcome, ProbeOutcome) and isinstance(outcome.code, ProbeCode):
        return outcome
    return ProbeOutcome(ProbeCode.NOT_CHECKED)


async def _guarded_ollama(
    configuration: Settings,
    transport: httpx.AsyncBaseTransport | None,
    *,
    deadline_seconds: float,
) -> ReadinessCheck:
    try:
        outcome = await asyncio.wait_for(
            _probe_ollama(configuration, transport), timeout=deadline_seconds,
        )
    except TimeoutError:
        outcome = ProbeOutcome(ProbeCode.TIMEOUT)
    except Exception as exc:
        log.warning("content_readiness_probe_failed", check="ollama", error=type(exc).__name__)
        outcome = ProbeOutcome(ProbeCode.NOT_CHECKED)
    return _build(_OLLAMA, _coerce(outcome))


async def collect_content_readiness(
    configuration: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    local_deadline_seconds: float = LOCAL_DEADLINE_SECONDS,
    ollama_deadline_seconds: float = OLLAMA_DEADLINE_SECONDS,
) -> ReadinessReport:
    """Inspect the current Settings. No database writes, inference or downloads."""
    config = configuration or get_settings()
    started = datetime.now(UTC)
    tts = config.content_tts_backend != "disabled"
    visual = config.content_visual_backend != "disabled"
    caption = config.content_caption_backend != "disabled"
    whisper = config.content_caption_backend == FASTER_WHISPER_QUALITY_TEST_BACKEND
    assembly = config.content_assembly_backend != "disabled"

    # The three narration probes inspect Kokoro and eSpeak NG; another narration
    # backend does not use them and has no probe of its own yet.
    kokoro = config.content_tts_backend == KOKORO_QUALITY_TEST_BACKEND
    tts_off = (
        "O backend de narração selecionado não usa Kokoro nem eSpeak NG; nada foi consultado."
        if tts
        else "A narração está desabilitada (CONTENT_TTS_BACKEND); nada foi consultado."
    )
    whisper_off = (
        "O backend de legendas selecionado não usa o modelo de transcrição; nada foi consultado."
        if caption
        else "As legendas estão desabilitadas (CONTENT_CAPTION_BACKEND); nada foi consultado."
    )
    assembly_off = "A montagem está desabilitada (CONTENT_ASSEMBLY_BACKEND); nada foi consultado."
    visual_off = "Os visuais estão desabilitados (CONTENT_VISUAL_BACKEND); nada foi consultado."

    # (spec, enabled?, reason when not applicable, probe)
    plan: list[tuple[_Spec, bool, str, Callable[[], ProbeOutcome]]] = [
        (_STORAGE, True, "", partial(checks.probe_storage, config.storage_root)),
        (_TTS_PACKAGES, kokoro, tts_off,
         partial(checks.probe_tts_packages, kokoro_version=KOKORO_PACKAGE_VERSION)),
        (_TTS_ESPEAK, kokoro, tts_off, checks.probe_espeak),
        (_TTS_CACHE, kokoro, tts_off, partial(
            checks.probe_tts_cache,
            voice_id=config.content_tts_voice_id,
            allowed_voices=tuple(KOKORO_PT_BR_VOICE_SHA256_PREFIXES),
            repository=KOKORO_MODEL_REPOSITORY,
            revision=KOKORO_MODEL_REVISION,
        )),
        (_CAPTION_PACKAGES, whisper, whisper_off, partial(
            checks.probe_caption_packages,
            faster_whisper_version=FASTER_WHISPER_PACKAGE_VERSION,
            ctranslate2_version=CTRANSLATE2_PACKAGE_VERSION,
        )),
        (_CAPTION_MODEL, whisper, whisper_off, partial(
            checks.probe_caption_model,
            config.content_caption_model_path or "",
            caption_snapshot_file_sizes(),
        )),
        (_FFMPEG, assembly, assembly_off, partial(
            checks.probe_media_tool,
            tool="ffmpeg",
            configured_path=config.content_ffmpeg_path,
            expected_sha256=config.content_ffmpeg_expected_sha256,
            required_flags=FFMPEG_REQUIRED_BUILD_FLAGS,
        )),
        (_FFPROBE, assembly, assembly_off, partial(
            checks.probe_media_tool,
            tool="ffprobe",
            configured_path=config.content_ffprobe_path,
            expected_sha256=config.content_ffprobe_expected_sha256,
        )),
        (_VISUAL_CONFIG, visual, visual_off, partial(
            checks.probe_visual_import_config,
            config.content_visual_import_root,
            config.content_visual_manifest_path,
        )),
    ]

    async def run(spec: _Spec, enabled: bool, reason: str,
                  probe: Callable[[], ProbeOutcome]) -> ReadinessCheck:
        if not enabled:
            return _not_applicable(spec, reason)
        return await _guarded(spec, probe, deadline_seconds=local_deadline_seconds)

    results = await asyncio.gather(
        _guarded_ollama(config, transport, deadline_seconds=ollama_deadline_seconds),
        *(run(*entry) for entry in plan),
    )

    conflicts = _pipeline_conflicts(config)
    pipeline = _build(_PIPELINE, ProbeOutcome(
        ProbeCode.INVALID_CONFIG if conflicts else ProbeCode.OK,
    ))
    if conflicts:
        pipeline = replace(
            pipeline,
            guidance=(
                "Configuração incoerente: " + " ".join(conflicts)
                + " Ajuste as variáveis CONTENT_* correspondentes no .env."
            ),
        )
    deferred = (
        _build(_VISUAL_INPUTS, ProbeOutcome(ProbeCode.DEFERRED)) if visual
        else _not_applicable(_VISUAL_INPUTS, visual_off),
        _build(_GPU_RUNTIME, ProbeOutcome(ProbeCode.DEFERRED)) if (tts or whisper)
        else _not_applicable(
            _GPU_RUNTIME,
            "Nenhum backend habilitado usa a GPU; nada foi consultado.",
        ),
    )
    return ReadinessReport(started, datetime.now(UTC), (pipeline, *results, *deferred))
