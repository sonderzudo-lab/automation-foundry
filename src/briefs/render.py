"""Deterministic Markdown rendering of the operations evidence document."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.briefs.evidence import EVIDENCE_SCHEMA_VERSION

_STATUS_ORDER = ("succeeded", "failed", "cancelled", "awaiting_approval", "running", "queued")


class OperationsBriefRenderError(ValueError):
    """Raised when evidence does not match the schema this renderer understands."""


def render_operations_brief(evidence: dict[str, Any]) -> str:
    """Render the brief. The same evidence always produces the same text."""
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise OperationsBriefRenderError("unsupported evidence schema")
    try:
        return _render(evidence)
    except (KeyError, TypeError) as exc:
        raise OperationsBriefRenderError("evidence is incomplete") from exc


def _render(evidence: dict[str, Any]) -> str:
    window = evidence["window"]
    runs = evidence["runs"]
    approvals = evidence["approvals"]
    alerts = evidence["alerts"]
    lines: list[str] = [
        "# Brief operacional do Automation Foundry",
        "",
        f"Janela: {window['days']} dia(s), de {window['start']} (inclusive) "
        f"a {window['end_exclusive']} (exclusive), em UTC.",
        f"Estado coletado em {evidence['collected_at']}.",
        "",
        "## Resumo",
        "",
        f"- Runs na janela: {runs['total']}; falhas: {runs['failed_total']}.",
        f"- Approvals pendentes agora: {approvals['pending_count']}.",
        f"- Alertas ativos agora: {alerts['active_count']}.",
    ]
    if runs["total"] == 0:
        lines.append("- Não houve atividade de runs nesta janela.")
    if runs["truncated"]:
        lines.append("- Atenção: a contagem de runs foi truncada pelo limite de leitura.")

    lines += ["", "## Runs por automação", ""]
    if runs["by_automation"]:
        lines += [
            "| Automação | Total | "
            + " | ".join(_STATUS_ORDER)
            + " | Taxa de sucesso | Duração média (s) |",
            "|---|---|" + "---|" * len(_STATUS_ORDER) + "---|---|",
        ]
        for item in runs["by_automation"]:
            counts = " | ".join(str(item["by_status"].get(name, 0)) for name in _STATUS_ORDER)
            lines.append(
                f"| {item['slug']} | {item['total']} | {counts} | "
                f"{_cell(item['success_rate'])} | {_cell(item['mean_duration_seconds'])} |"
            )
        lines += [
            "",
            "A taxa de sucesso considera apenas runs `succeeded` e `failed`.",
        ]
    else:
        lines.append("Nenhuma run registrada na janela.")

    lines += ["", "## Falhas recentes", ""]
    if runs["recent_failures"]:
        lines += [
            f"- Run #{item['run_id']} · {item['automation']} · {item['error_code']}"
            for item in runs["recent_failures"]
        ]
    else:
        lines.append("Nenhuma falha na janela.")

    lines += ["", "## Approvals", ""]
    lines.append(
        f"Decididas na janela: {approvals['decided_in_window']} "
        f"(aprovadas {approvals['approved_in_window']}, "
        f"rejeitadas {approvals['rejected_in_window']}); "
        f"tempo médio de decisão: {_decision_time(approvals['mean_decision_seconds'], evidence)}."
    )
    if approvals["pending"]:
        lines += ["", "Pendentes, da mais antiga para a mais recente:", ""]
        lines += [
            f"- Approval #{item['approval_id']} · run #{item['run_id']} · "
            f"{item['automation']} · {item['action']} · {_age(item['age_seconds'])}"
            for item in approvals["pending"]
        ]
        if approvals["pending_count"] > len(approvals["pending"]):
            lines.append(f"- ... e mais {approvals['pending_count'] - len(approvals['pending'])}.")
    else:
        lines += ["", "Nenhuma approval pendente."]

    lines += ["", "## Alertas", ""]
    severity = ", ".join(f"{name}: {count}" for name, count in alerts["active_by_severity"].items())
    lines.append(
        f"Ativos agora: {alerts['active_count']}"
        + (f" ({severity})" if severity else "")
        + f"; abertos na janela: {alerts['opened_in_window']}."
    )

    lines += ["", "## Custos e resultados observados", ""]
    if evidence["ledger"]:
        lines += [
            "| Moeda | Custo | Receita | Valor atribuído | Receita líquida |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {item['currency']} | {item['cost']} | {item['revenue']} | "
            f"{item['attributed_value']} | {item['net_revenue']} |"
            for item in evidence["ledger"]
        ]
        lines += [
            "",
            "Valores são observações do ledger local, sem conversão cambial, pagamento ou promessa.",
        ]
    else:
        lines.append("Nenhuma observação financeira na janela.")

    lines += ["", "## Connectors", ""]
    connectors = evidence["connectors"]
    if connectors["latest"]:
        lines += [
            f"- {item['automation']} · {item['connector_key']} · {item['status']} · "
            f"qualidade {item['quality_status']} · "
            f"{'desatualizado' if item['stale'] else 'atualizado'}"
            for item in connectors["latest"]
        ]
    else:
        lines.append("Nenhuma observação de connector registrada.")

    lines += [
        "",
        "## Limitações",
        "",
        "- Este brief descreve somente dados persistidos no banco local; não prevê resultados.",
        "- Não inclui payloads, mensagens de erro, textos de revisão nem origem de métricas.",
        "- Nada foi enviado, publicado ou gasto para produzi-lo.",
        "",
    ]
    return "\n".join(lines)


def _decision_time(value: str | None, evidence: dict[str, Any]) -> str:
    if int(evidence.get("renderer_version", 1)) < 2:
        return f"{_cell(value)} s"
    return "—" if value is None else _human_duration(value)


def _human_duration(seconds: str) -> str:
    days, remainder = divmod(int(Decimal(seconds)), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def _cell(value: str | None) -> str:
    return "—" if value is None else value


def _age(seconds: int) -> str:
    days, remainder = divmod(seconds, 86_400)
    hours = remainder // 3_600
    return f"{days} d {hours} h" if days else f"{hours} h"
