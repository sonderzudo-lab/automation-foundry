"""Declarative retention policy for every artifact type this project produces.

This module is the single place that decides how long a produced artifact stays
eligible to exist. It stores no state, touches no filesystem and deletes
nothing: producers resolve their retention here at registration time, and
`register_local_artifact` freezes `retention_until` into the row. Changing a
value here therefore affects only artifacts registered afterwards; artifacts
already recorded keep the deadline they were registered with.

An artifact type that is not declared here resolves to indefinite retention.
That is the fail-closed direction for a project whose only deletion path is the
approved purge: an undeclared type never expires, so it can never enter a purge
plan by omission.
"""

from __future__ import annotations

from dataclasses import dataclass

UNDECLARED_PRODUCER = "undeclared"
_UNDECLARED_RATIONALE = (
    "Tipo não declarado na política. A retenção fica indefinida para que o "
    "artefato nunca expire nem entre em um plano de expurgo por omissão."
)


@dataclass(frozen=True, slots=True)
class RetentionPolicyEntry:
    """One declared retention decision for a single artifact type."""

    artifact_type: str
    retention_days: int | None
    producer: str
    rationale: str

    @property
    def declared(self) -> bool:
        """Return whether this entry comes from the declared policy table."""
        return self.producer != UNDECLARED_PRODUCER

    @property
    def indefinite(self) -> bool:
        """Return whether the artifact never reaches a retention deadline."""
        return self.retention_days is None


_POLICY: tuple[RetentionPolicyEntry, ...] = (
    RetentionPolicyEntry(
        artifact_type="script_bundle",
        retention_days=90,
        producer="content-engine:a1",
        rationale=(
            "Roteiro aprovado: evidência da approval editorial A1 e base de "
            "comparação do gate de originalidade A6."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="narration_audio",
        retention_days=90,
        producer="content-engine:a2",
        rationale=(
            "Narração WAV: entrada verificada de A3, A4 e A5 e referência do "
            "diagnóstico de alinhamento."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="visual_manifest",
        retention_days=90,
        producer="content-engine:a3",
        rationale=(
            "Manifesto dos visuais: prova de proveniência, licença e direito "
            "comercial declarado por asset."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="visual_image",
        retention_days=90,
        producer="content-engine:a3",
        rationale=(
            "Imagens do vídeo: também são o conjunto congelado de candidatos "
            "da escolha de thumbnail A8."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="caption_ass",
        retention_days=90,
        producer="content-engine:a4",
        rationale=(
            "Legenda ASS: evidência do texto queimado no vídeo final e entrada "
            "do diagnóstico de alinhamento."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="caption_alignment_report",
        retention_days=90,
        producer="content-engine:a4-alignment",
        rationale=(
            "Relatório de alinhamento: diagnóstico somente leitura do "
            "sincronismo entre áudio e legenda daquela run."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="final_video",
        retention_days=90,
        producer="content-engine:a5",
        rationale=(
            "Vídeo final: objeto revisado pelo gate humano A7 e servido pelo "
            "export local verificado."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="originality_report",
        retention_days=90,
        producer="content-engine:a6",
        rationale=(
            "Relatório de originalidade: evidência de que o gate A6 avaliou "
            "exatamente aquele roteiro e aquele vídeo."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="operations_brief",
        retention_days=180,
        producer="operations-brief",
        rationale=(
            "Brief operacional aprovado: histórico comparável de semana a "
            "semana para avaliar o módulo antes de qualquer schedule."
        ),
    ),
    RetentionPolicyEntry(
        artifact_type="operations_brief_evidence",
        retention_days=180,
        producer="operations-brief",
        rationale=(
            "Evidência estruturada do brief: prova de onde cada número veio e "
            "base da conferência de integridade na revisão."
        ),
    ),
)


def _validate(policy: tuple[RetentionPolicyEntry, ...]) -> tuple[RetentionPolicyEntry, ...]:
    seen: set[str] = set()
    for entry in policy:
        if not entry.artifact_type.strip() or entry.artifact_type != entry.artifact_type.strip():
            raise ValueError("artifact_type must be a non-empty, trimmed identifier")
        if entry.artifact_type in seen:
            raise ValueError(f"duplicated retention policy for {entry.artifact_type}")
        seen.add(entry.artifact_type)
        if entry.retention_days is not None and entry.retention_days < 1:
            raise ValueError("retention_days must be at least 1 day or indefinite")
        if entry.producer == UNDECLARED_PRODUCER or not entry.producer.strip():
            raise ValueError("declared policy requires a real producer identifier")
        if not entry.rationale.strip():
            raise ValueError("declared policy requires an explicit rationale")
    return policy


_POLICY = _validate(_POLICY)
_BY_TYPE = {entry.artifact_type: entry for entry in _POLICY}


def resolve_retention_policy(artifact_type: str) -> RetentionPolicyEntry:
    """Return the declared policy or an indefinite fallback for unknown types."""
    normalized = artifact_type.strip()
    if not normalized:
        raise ValueError("artifact_type must not be empty")
    declared = _BY_TYPE.get(normalized)
    if declared is not None:
        return declared
    return RetentionPolicyEntry(
        artifact_type=normalized,
        retention_days=None,
        producer=UNDECLARED_PRODUCER,
        rationale=_UNDECLARED_RATIONALE,
    )


def resolve_retention_days(artifact_type: str) -> int | None:
    """Return the declared retention in days, or None for indefinite retention."""
    return resolve_retention_policy(artifact_type).retention_days


def declared_retention_policies() -> tuple[RetentionPolicyEntry, ...]:
    """Return every declared entry ordered by artifact type."""
    return tuple(sorted(_POLICY, key=lambda entry: entry.artifact_type))
