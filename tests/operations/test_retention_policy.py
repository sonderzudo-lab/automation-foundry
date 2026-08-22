"""Declarative retention policy per artifact type, resolved before registration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.intelligence import similarity
from src.operations.retention_policy import (
    UNDECLARED_PRODUCER,
    RetentionPolicyEntry,
    declared_retention_policies,
    resolve_retention_days,
    resolve_retention_policy,
)
from src.pipeline import a1_executor, assembly, caption_alignment, captions, tts, visuals

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
_ARTIFACT_TYPE_LITERAL = re.compile(r"""artifact_type=["']([a-z0-9_]+)["']""")


def test_every_declared_entry_is_explicit_and_unique() -> None:
    entries = declared_retention_policies()

    assert entries
    assert len({entry.artifact_type for entry in entries}) == len(entries)
    assert [entry.artifact_type for entry in entries] == sorted(
        entry.artifact_type for entry in entries
    )
    for entry in entries:
        assert entry.declared is True
        assert entry.producer != UNDECLARED_PRODUCER
        assert entry.rationale.strip()
        assert entry.retention_days is None or entry.retention_days >= 1


def test_unknown_types_resolve_to_indefinite_retention() -> None:
    entry = resolve_retention_policy("some_future_artifact")

    assert entry.declared is False
    assert entry.indefinite is True
    assert entry.retention_days is None
    assert entry.producer == UNDECLARED_PRODUCER
    assert resolve_retention_days("some_future_artifact") is None


def test_resolution_normalizes_input_and_rejects_empty_types() -> None:
    assert resolve_retention_days("  script_bundle  ") == 90

    with pytest.raises(ValueError, match="artifact_type"):
        resolve_retention_policy("   ")


def test_every_artifact_type_produced_in_src_is_declared() -> None:
    declared = {entry.artifact_type for entry in declared_retention_policies()}
    produced: set[str] = set()
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        produced.update(
            _ARTIFACT_TYPE_LITERAL.findall(path.read_text(encoding="utf-8"))
        )

    assert produced
    assert produced <= declared, f"undeclared artifact types: {sorted(produced - declared)}"


def test_producers_resolve_their_retention_from_the_policy() -> None:
    assert resolve_retention_days("script_bundle") == a1_executor._ARTIFACT_RETENTION_DAYS
    assert resolve_retention_days("narration_audio") == tts._RETENTION_DAYS
    assert resolve_retention_days("visual_manifest") == visuals._MANIFEST_RETENTION_DAYS
    assert resolve_retention_days("visual_image") == visuals._IMAGE_RETENTION_DAYS
    assert resolve_retention_days("caption_ass") == captions._RETENTION_DAYS
    assert (
        resolve_retention_days("caption_alignment_report")
        == caption_alignment._RETENTION_DAYS
    )
    assert resolve_retention_days("final_video") == assembly._RETENTION_DAYS
    assert resolve_retention_days("originality_report") == similarity._RETENTION_DAYS


def test_current_policy_preserves_the_ninety_day_pipeline_baseline() -> None:
    """The declaration must not silently shorten what the pipeline already used."""
    expected = {
        "script_bundle": 90,
        "narration_audio": 90,
        "visual_manifest": 90,
        "visual_image": 90,
        "caption_ass": 90,
        "caption_alignment_report": 90,
        "final_video": 90,
        "originality_report": 90,
    }

    assert {
        entry.artifact_type: entry.retention_days
        for entry in declared_retention_policies()
    } == expected


@pytest.mark.parametrize(
    "entry",
    [
        RetentionPolicyEntry("script_bundle", 0, "content-engine:a1", "x"),
        RetentionPolicyEntry("", 30, "content-engine:a1", "x"),
        RetentionPolicyEntry("script_bundle", 30, UNDECLARED_PRODUCER, "x"),
        RetentionPolicyEntry("script_bundle", 30, "content-engine:a1", "  "),
    ],
)
def test_policy_validation_rejects_incoherent_entries(
    entry: RetentionPolicyEntry,
) -> None:
    from src.operations.retention_policy import _validate

    with pytest.raises(ValueError):
        _validate((entry,))


def test_policy_validation_rejects_duplicated_types() -> None:
    from src.operations.retention_policy import _validate

    duplicated = (
        RetentionPolicyEntry("script_bundle", 30, "content-engine:a1", "x"),
        RetentionPolicyEntry("script_bundle", 60, "content-engine:a1", "y"),
    )

    with pytest.raises(ValueError, match="duplicated"):
        _validate(duplicated)
