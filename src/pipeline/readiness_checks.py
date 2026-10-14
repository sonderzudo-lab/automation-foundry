"""Light, read-only local probes for the Content Engine readiness diagnostic.

Each probe answers one question with a closed ``ProbeCode`` and never returns a path,
an exception message or an endpoint. Probes mirror what the real adapters demand, but
they never import a model runtime, hash weights, write, download or run inference.
They are synchronous and cheap; the caller runs them under its own deadline.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Advisory floor, not an estimate of what one video needs.
STORAGE_MIN_FREE_BYTES = 1024**3
TOOL_VERSION_TIMEOUT_SECONDS = 3.0
_TOOL_OUTPUT_LIMIT = 64 * 1024
_HEX_DIGITS = frozenset("0123456789abcdef")
_OFFLINE_VALUES = frozenset({"1", "true", "yes", "on"})
_TTS_REQUIRED_MODULES = ("kokoro", "torch", "numpy", "huggingface_hub")
_TTS_CACHED_FILES = ("config.json", "kokoro-v1_0.pth")


class ProbeCode(StrEnum):
    """Closed, stable outcome codes; part of the operator-visible contract."""

    OK = "OK"
    DISABLED = "DISABLED"
    INVALID_CONFIG = "INVALID_CONFIG"
    MISSING = "MISSING"
    INCOMPLETE = "INCOMPLETE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNAVAILABLE = "UNAVAILABLE"
    LOW_SPACE = "LOW_SPACE"
    TIMEOUT = "TIMEOUT"
    NOT_CHECKED = "NOT_CHECKED"
    DEFERRED = "DEFERRED"


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    code: ProbeCode
    free_bytes: int | None = None


def _is_sha256(value: str | None) -> bool:
    candidate = value.strip().casefold() if isinstance(value, str) else ""
    return len(candidate) == 64 and all(character in _HEX_DIGITS for character in candidate)


def _first_byte_readable(path: Path) -> bool:
    with path.open("rb") as stream:
        return bool(stream.read(1))


def probe_storage(root: str, *, min_free_bytes: int = STORAGE_MIN_FREE_BYTES) -> ProbeOutcome:
    """Root exists, can be listed and has free space. Real writes are not attempted."""
    if not root.strip():
        return ProbeOutcome(ProbeCode.INVALID_CONFIG)
    directory = Path(root).expanduser()
    try:
        if not directory.exists():
            return ProbeOutcome(ProbeCode.MISSING)
        if not directory.is_dir():
            return ProbeOutcome(ProbeCode.INVALID_CONFIG)
        if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
            return ProbeOutcome(ProbeCode.UNAVAILABLE)
        with os.scandir(directory) as entries:
            next(entries, None)  # os.access is unreliable on Windows; prove it can be read
        free = shutil.disk_usage(directory).free
    except OSError:
        return ProbeOutcome(ProbeCode.UNAVAILABLE)
    return ProbeOutcome(ProbeCode.LOW_SPACE if free < min_free_bytes else ProbeCode.OK, free)


def probe_tts_packages(*, kokoro_version: str) -> ProbeOutcome:
    """Pinned ``kokoro`` distribution and the modules the adapter imports are installed.

    Module presence uses ``find_spec`` (no import) because distribution names differ
    from module names, e.g. ``phonemizer-fork`` provides ``phonemizer``.
    """
    try:
        installed = importlib.metadata.version("kokoro")
    except importlib.metadata.PackageNotFoundError:
        return ProbeOutcome(ProbeCode.MISSING)
    if installed != kokoro_version:
        return ProbeOutcome(ProbeCode.INCOMPATIBLE)
    for module in _TTS_REQUIRED_MODULES:
        try:
            if importlib.util.find_spec(module) is None:
                return ProbeOutcome(ProbeCode.MISSING)
        except (ImportError, ValueError):
            return ProbeOutcome(ProbeCode.MISSING)
    return ProbeOutcome(ProbeCode.OK)


def probe_espeak() -> ProbeOutcome:
    """The adapter only checks that ``espeak-ng`` is resolvable on PATH."""
    return ProbeOutcome(ProbeCode.OK if shutil.which("espeak-ng") else ProbeCode.MISSING)


def probe_tts_cache(
    *,
    voice_id: str,
    allowed_voices: Sequence[str],
    repository: str,
    revision: str,
) -> ProbeOutcome:
    """Pinned Kokoro files already in the local Hugging Face cache; never downloads.

    The step fetches missing files with ``hf_hub_download``. An absent cache is
    therefore ``DEFERRED`` (first narration downloads it) unless offline mode is set,
    in which case the step could not fetch it and the absence is a real problem.
    """
    if voice_id not in allowed_voices:
        return ProbeOutcome(ProbeCode.INVALID_CONFIG)
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return ProbeOutcome(ProbeCode.MISSING)
    complete = True
    for filename in (*_TTS_CACHED_FILES, f"voices/{voice_id}.pt"):
        cached = try_to_load_from_cache(repository, filename, revision=revision)
        if not isinstance(cached, str):
            complete = False
            continue
        try:
            if not Path(cached).is_file() or not _first_byte_readable(Path(cached)):
                complete = False
        except OSError:
            return ProbeOutcome(ProbeCode.UNAVAILABLE)
    if complete:
        return ProbeOutcome(ProbeCode.OK)
    offline = os.environ.get("HF_HUB_OFFLINE", "").strip().casefold() in _OFFLINE_VALUES
    return ProbeOutcome(ProbeCode.MISSING if offline else ProbeCode.DEFERRED)


def probe_caption_packages(
    *, faster_whisper_version: str, ctranslate2_version: str,
) -> ProbeOutcome:
    for distribution, expected in (
        ("faster-whisper", faster_whisper_version),
        ("ctranslate2", ctranslate2_version),
    ):
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            return ProbeOutcome(ProbeCode.MISSING)
        if installed != expected:
            return ProbeOutcome(ProbeCode.INCOMPATIBLE)
    return ProbeOutcome(ProbeCode.OK)


def probe_caption_model(
    root: str, expected_files: Sequence[tuple[str, int]],
) -> ProbeOutcome:
    """Snapshot files exist, are readable and have the pinned size.

    Mirrors the step's own checks (``is_file`` and size, following symlinks as a
    Hugging Face snapshot layout uses). Hashes stay with the step: ``model.bin``
    alone is hundreds of megabytes.
    """
    if not root.strip():
        return ProbeOutcome(ProbeCode.INVALID_CONFIG)
    directory = Path(root).expanduser()
    try:
        if not directory.exists():
            return ProbeOutcome(ProbeCode.MISSING)
        if not directory.is_dir():
            return ProbeOutcome(ProbeCode.INVALID_CONFIG)
        present = 0
        for name, size in expected_files:
            candidate = directory / name
            if candidate.is_file():
                present += 1
                if candidate.stat().st_size != size or not _first_byte_readable(candidate):
                    return ProbeOutcome(ProbeCode.INCOMPLETE)
    except OSError:
        return ProbeOutcome(ProbeCode.UNAVAILABLE)
    if present == len(expected_files):
        return ProbeOutcome(ProbeCode.OK)
    return ProbeOutcome(ProbeCode.MISSING if present == 0 else ProbeCode.INCOMPLETE)


def probe_media_tool(
    *,
    tool: str,
    configured_path: str,
    expected_sha256: str | None,
    required_flags: Sequence[str] = (),
) -> ProbeOutcome:
    """Configured executable exists and answers ``-version`` like the expected tool.

    Config is checked first so an invalid pin never executes anything. The pinned hash
    is only checked for format here; hashing the executable is left to the step,
    which verifies it before use.
    """
    if not configured_path.strip() or not _is_sha256(expected_sha256):
        return ProbeOutcome(ProbeCode.INVALID_CONFIG)
    discovered = shutil.which(configured_path.strip())
    try:
        resolved = Path(discovered or configured_path.strip()).resolve(strict=True)
        if not resolved.is_file():
            return ProbeOutcome(ProbeCode.MISSING)
    except (OSError, RuntimeError):
        return ProbeOutcome(ProbeCode.MISSING)
    try:
        completed = subprocess.run(
            [str(resolved), "-hide_banner", "-version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TOOL_VERSION_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return ProbeOutcome(ProbeCode.TIMEOUT)
    except OSError:
        return ProbeOutcome(ProbeCode.UNAVAILABLE)
    output = completed.stdout[:_TOOL_OUTPUT_LIMIT].decode("utf-8", errors="replace")
    if completed.returncode != 0 or f"{tool} version " not in output.casefold():
        return ProbeOutcome(ProbeCode.INCOMPATIBLE)
    if any(flag not in output for flag in required_flags):
        return ProbeOutcome(ProbeCode.INCOMPATIBLE)
    return ProbeOutcome(ProbeCode.OK)


def probe_visual_import_config(root: str, manifest: str) -> ProbeOutcome:
    """Import root and manifest are a coherent, contained configuration.

    Mirrors ``build_local_asset_adapter``: root is not a filesystem anchor and the
    manifest, relative or absolute, resolves inside it. Whether the manifest and the
    images exist is a run input that depends on the narration duration; it is
    reported separately as deferred and never judged here.
    """
    if not root.strip() or not manifest.strip():
        return ProbeOutcome(ProbeCode.INVALID_CONFIG)
    try:
        import_root = Path(root).expanduser().resolve()
        manifest_path = Path(manifest).expanduser()
        if not manifest_path.is_absolute():
            manifest_path = import_root / manifest_path
        manifest_path = manifest_path.resolve()
        if import_root == Path(import_root.anchor):
            return ProbeOutcome(ProbeCode.INVALID_CONFIG)
        if manifest_path == import_root or not manifest_path.is_relative_to(import_root):
            return ProbeOutcome(ProbeCode.INVALID_CONFIG)
        if import_root.exists() and not import_root.is_dir():
            return ProbeOutcome(ProbeCode.INVALID_CONFIG)
        if manifest_path.exists() and not manifest_path.is_file():
            return ProbeOutcome(ProbeCode.INVALID_CONFIG)
    except (OSError, RuntimeError, ValueError):
        return ProbeOutcome(ProbeCode.UNAVAILABLE)
    return ProbeOutcome(ProbeCode.OK)
