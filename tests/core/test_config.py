"""Tests for safe, local-first configuration defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings


def test_defaults_use_automation_foundry_identity_and_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default infrastructure endpoints stay local and use the current project name."""
    for variable in (
        "DASHBOARD_HOST",
        "DASHBOARD_PORT",
        "DATABASE_URL",
        "OLLAMA_BASE_URL",
        "REDDIT_USER_AGENT",
        "REDIS_URL",
        "CONTENT_TTS_BACKEND",
        "CONTENT_NARRATION_REVIEW_ENABLED",
        "CONTENT_VISUAL_BACKEND",
        "CONTENT_VISUAL_IMPORT_ROOT",
        "CONTENT_VISUAL_MANIFEST_PATH",
        "CONTENT_CAPTION_BACKEND",
        "CONTENT_CAPTION_ALIGNMENT_GATE_ENABLED",
        "CONTENT_ASSEMBLY_BACKEND",
        "CONTENT_FFMPEG_PATH",
        "CONTENT_FFPROBE_PATH",
        "CONTENT_FFMPEG_EXPECTED_SHA256",
        "CONTENT_FFPROBE_EXPECTED_SHA256",
        "CONTENT_SIMILARITY_THRESHOLD",
        "CONTENT_SIMILARITY_WINDOW",
        "CONTENT_FINAL_REVIEW_ENABLED",
        "CONTENT_THUMBNAIL_REVIEW_ENABLED",
    ):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite+aiosqlite:///./automation_foundry.db"
    assert settings.dashboard_host == "127.0.0.1"
    assert settings.dashboard_port == 8000
    assert settings.ollama_base_url == "http://127.0.0.1:11434/v1"
    assert settings.reddit_user_agent == "automation-foundry/0.1"
    assert settings.redis_url == "redis://127.0.0.1:6379/0"
    assert settings.content_tts_backend == "disabled"
    assert settings.content_tts_voice_id == "pf_dora"
    assert settings.content_narration_review_enabled is False
    assert settings.content_visual_backend == "disabled"
    assert settings.content_visual_import_root == "./imports/content-visuals"
    assert settings.content_visual_manifest_path == "manifest.json"
    assert settings.content_caption_backend == "disabled"
    assert settings.content_caption_alignment_gate_enabled is False
    assert settings.content_caption_alignment_min_speech_coverage == 0.90
    assert settings.content_caption_alignment_max_outside_speech == 0.25
    assert settings.content_caption_alignment_tolerance_seconds == 0.50
    assert settings.content_assembly_backend == "disabled"
    assert settings.content_ffmpeg_path == "ffmpeg"
    assert settings.content_ffprobe_path == "ffprobe"
    assert settings.content_similarity_threshold == 0.85
    assert settings.content_similarity_window == 20
    assert settings.content_final_review_enabled is False
    assert settings.content_thumbnail_review_enabled is False


def test_thumbnail_review_requires_final_review() -> None:
    with pytest.raises(ValidationError, match="requires A7"):
        Settings(
            _env_file=None,
            content_final_review_enabled=False,
            content_thumbnail_review_enabled=True,
        )

    settings = Settings(
        _env_file=None,
        content_final_review_enabled=True,
        content_thumbnail_review_enabled=True,
    )
    assert settings.content_thumbnail_review_enabled is True


def test_tts_backend_rejects_unscoped_kokoro_mode() -> None:
    with pytest.raises(ValidationError, match="content_tts_backend"):
        Settings(_env_file=None, content_tts_backend="kokoro")


def test_narration_review_requires_enabled_tts() -> None:
    with pytest.raises(ValidationError, match="narration review requires"):
        Settings(
            _env_file=None,
            content_tts_backend="disabled",
            content_narration_review_enabled=True,
        )

    settings = Settings(
        _env_file=None,
        content_tts_backend="kokoro_quality_test",
        content_tts_voice_id="pm_alex",
        content_visual_backend="local_assets_quality_test",
        content_narration_review_enabled=True,
    )
    assert settings.content_narration_review_enabled is True

    with pytest.raises(ValidationError, match="requires A3"):
        Settings(
            _env_file=None,
            content_tts_backend="kokoro_quality_test",
            content_tts_voice_id="pm_alex",
            content_narration_review_enabled=True,
        )


def test_tts_quality_test_accepts_only_reviewed_pt_br_profiles() -> None:
    settings = Settings(
        _env_file=None,
        content_tts_backend="kokoro_quality_test",
        content_tts_voice_id="pm_alex",
        content_tts_language_code="p",
    )

    assert settings.content_tts_backend == "kokoro_quality_test"

    with pytest.raises(ValidationError, match="language code"):
        Settings(
            _env_file=None,
            content_tts_backend="kokoro_quality_test",
            content_tts_voice_id="pf_dora",
            content_tts_language_code="a",
        )
    with pytest.raises(ValidationError, match="voice ID"):
        Settings(
            _env_file=None,
            content_tts_backend="kokoro_quality_test",
            content_tts_voice_id="af_heart",
            content_tts_language_code="p",
        )


@pytest.mark.parametrize(
    "backend",
    ["flux", "flux_schnell", "flux_schnell_quality_test", "flux_dev"],
)
def test_visual_backend_remains_fail_closed_after_flux_audit(backend: str) -> None:
    with pytest.raises(ValidationError, match="content_visual_backend"):
        Settings(_env_file=None, content_visual_backend=backend)


def test_local_visual_asset_quality_test_is_explicitly_selectable() -> None:
    settings = Settings(
        _env_file=None,
        content_visual_backend="local_assets_quality_test",
        content_visual_import_root="./operator-imports",
        content_visual_manifest_path="reviewed.json",
    )

    assert settings.content_visual_backend == "local_assets_quality_test"
    assert settings.content_visual_import_root == "./operator-imports"
    assert settings.content_visual_manifest_path == "reviewed.json"


def test_caption_backend_remains_fail_closed_until_model_audit() -> None:
    with pytest.raises(ValidationError, match="content_caption_backend"):
        Settings(_env_file=None, content_caption_backend="faster_whisper")


def test_approved_text_timing_quality_test_is_explicitly_selectable() -> None:
    settings = Settings(
        _env_file=None,
        content_caption_backend="approved_text_timing_quality_test",
    )

    assert settings.content_caption_backend == "approved_text_timing_quality_test"


def test_assembly_backend_remains_fail_closed_until_ffmpeg_audit() -> None:
    with pytest.raises(ValidationError, match="content_assembly_backend"):
        Settings(_env_file=None, content_assembly_backend="ffmpeg")


def test_ffmpeg_quality_test_requires_pinned_binary_hashes() -> None:
    digest = "a" * 64
    settings = Settings(
        _env_file=None,
        content_assembly_backend="ffmpeg_quality_test",
        content_ffmpeg_expected_sha256=digest,
        content_ffprobe_expected_sha256=digest,
    )
    assert settings.content_assembly_backend == "ffmpeg_quality_test"

    with pytest.raises(ValidationError, match="content_ffmpeg_expected_sha256"):
        Settings(
            _env_file=None,
            content_assembly_backend="ffmpeg_quality_test",
            content_ffmpeg_expected_sha256="",
            content_ffprobe_expected_sha256=digest,
        )


def test_caption_alignment_diagnostic_configuration_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_caption_alignment_min_speech_coverage=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_caption_alignment_min_speech_coverage=1.01)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_caption_alignment_max_outside_speech=-0.01)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_caption_alignment_max_outside_speech=1.01)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_caption_alignment_tolerance_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_caption_alignment_tolerance_seconds=30.01)


def test_caption_alignment_gate_requires_the_complete_a7_pipeline() -> None:
    with pytest.raises(ValidationError, match="requires A7"):
        Settings(_env_file=None, content_caption_alignment_gate_enabled=True)
    with pytest.raises(ValidationError, match="requires A4"):
        Settings(
            _env_file=None,
            content_caption_alignment_gate_enabled=True,
            content_final_review_enabled=True,
        )
    with pytest.raises(ValidationError, match="requires A5"):
        Settings(
            _env_file=None,
            content_caption_alignment_gate_enabled=True,
            content_final_review_enabled=True,
            content_caption_backend="approved_text_timing_quality_test",
        )

    configured = Settings(
        _env_file=None,
        content_caption_alignment_gate_enabled=True,
        content_final_review_enabled=True,
        content_caption_backend="approved_text_timing_quality_test",
        content_assembly_backend="ffmpeg_quality_test",
        content_ffmpeg_expected_sha256="a" * 64,
        content_ffprobe_expected_sha256="b" * 64,
    )
    assert configured.content_caption_alignment_gate_enabled is True


def test_similarity_gate_configuration_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_similarity_threshold=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_similarity_threshold=1.01)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_similarity_window=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_similarity_window=101)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com"])
def test_dashboard_rejects_non_loopback_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(_env_file=None, dashboard_host=host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.2", "::1"])
def test_dashboard_accepts_loopback_hosts(host: str) -> None:
    settings = Settings(_env_file=None, dashboard_host=host)

    assert settings.dashboard_host == host


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_postgresql_accepts_only_asyncpg_on_loopback(host: str) -> None:
    rendered_host = f"[{host}]" if ":" in host else host
    database_url = (
        f"postgresql+asyncpg://foundry:secret@{rendered_host}:5432/automation_foundry"
    )

    settings = Settings(_env_file=None, database_url=database_url)

    assert settings.database_url == database_url


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://foundry:secret@127.0.0.1/automation_foundry",
        "postgresql+asyncpg://foundry:secret@192.168.1.20/automation_foundry",
        "mysql+aiomysql://foundry:secret@127.0.0.1/automation_foundry",
    ],
)
def test_database_rejects_unsupported_or_non_loopback_urls(database_url: str) -> None:
    with pytest.raises(ValidationError, match="database_url|loopback"):
        Settings(_env_file=None, database_url=database_url)


def test_database_pool_limits_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_pool_size=0)


def test_health_capacity_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="warning threshold"):
        Settings(
            _env_file=None,
            health_warning_percent=95,
            health_critical_percent=90,
        )


def test_health_alert_thresholds_are_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, health_alert_failure_threshold=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, health_alert_recovery_threshold=101)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, celery_health_tick_seconds=9)


def test_runtime_timeouts_are_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runtime_startup_timeout_seconds=9)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runtime_shutdown_timeout_seconds=4)

    settings = Settings(
        _env_file=None,
        runtime_startup_timeout_seconds=10,
        runtime_shutdown_timeout_seconds=5,
    )

    assert settings.runtime_startup_timeout_seconds == 10
    assert settings.runtime_shutdown_timeout_seconds == 5


def test_retention_inventory_limits_are_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retention_inventory_max_files=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retention_inventory_max_items=0)

    settings = Settings(_env_file=None)

    assert settings.retention_inventory_max_files == 200_000
    assert settings.retention_inventory_max_items == 200
