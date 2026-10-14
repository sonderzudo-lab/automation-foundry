"""
Testes de scripts/experiments/e8_script_critic.py — crítico e roteirista mockados.

Cobrem as partes puras do experimento: checagens em código, leitura da resposta do
crítico, ciclo revisar→reescrever, escolha do melhor candidato e planilha cega.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from unittest.mock import MagicMock

import pytest

from scripts.experiments.e8_script_critic import (
    CHECKLIST,
    CriticResponseError,
    Entry,
    Finding,
    RefineResult,
    Review,
    Topic,
    blind_entries,
    code_findings,
    critic_prompts,
    critic_schema,
    parse_critic_response,
    pick_best,
    refine,
    render_answer_key,
    render_sheet,
    review_script,
    rewrite_script,
    split_hook,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_NARRATION = (
    "Sua lista de tarefas enorme não é organização, é adiamento disfarçado.\n\n"
    "Quando tudo está na lista, nada é prioridade. Você abre o caderno, vê vinte linhas "
    "e escolhe a mais fácil. Curta e se inscreva no canal."
)
_TOPIC = Topic("Por que uma lista de tarefas longa faz você render menos", "contrarian")


def _critic_json(**answers: bool | tuple[str, str]) -> str:
    """Resposta do crítico: True, ou (trecho, correção) para um 'não'."""
    checks = []
    for check_id, answer in answers.items():
        if answer is True:
            checks.append({"id": check_id, "ok": True, "quote": "", "fix": ""})
        else:
            assert isinstance(answer, tuple)
            checks.append({"id": check_id, "ok": False, "quote": answer[0], "fix": answer[1]})
    return json.dumps({"checks": checks})


def _all_ok() -> str:
    return _critic_json(**{check.id: True for check in CHECKLIST})


def _review(count: int, *, critic_answered: bool = True) -> Review:
    return Review(
        findings=tuple(Finding("critic", f"c{index}", "detalhe") for index in range(count)),
        critic_answered=critic_answered,
    )


def _scripted(
    reviews: dict[str, Review], rewrites: Sequence[str | None]
) -> tuple[MagicMock, MagicMock]:
    review = MagicMock(side_effect=lambda text: reviews[text])
    rewrite = MagicMock(side_effect=list(rewrites))
    return review, rewrite


def _llm_client(*responses: str) -> MagicMock:
    client = MagicMock()
    replies = []
    for content in responses:
        reply = MagicMock()
        reply.choices[0].message.content = content
        replies.append(reply)
    client.chat.completions.create.side_effect = replies
    return client


def _words(count: int) -> str:
    return "Abertura curta do vídeo.\n\n" + " ".join(["palavra"] * (count - 4)) + "."


# ── Checagens em código ───────────────────────────────────────────────────────


def test_split_hook_returns_first_paragraph() -> None:
    assert split_hook(_NARRATION) == (
        "Sua lista de tarefas enorme não é organização, é adiamento disfarçado."
    )


def test_code_findings_accepts_a_clean_script() -> None:
    assert code_findings(_NARRATION, opening_style="contrarian") == []


def test_code_findings_flags_long_hook_style_and_long_sentence() -> None:
    hook = "Você sabia que " + " ".join(["muito"] * 26) + "?"
    body = "Uma frase curta. " + " ".join(["e"] * 31) + "."
    codes = [
        finding.code
        for finding in code_findings(f"{hook}\n\n{body}", opening_style="contrarian")
    ]
    assert codes == ["hook_too_long", "opening_style", "long_sentence"]


# ── Resposta do crítico ───────────────────────────────────────────────────────


def test_parse_critic_response_without_problems() -> None:
    assert parse_critic_response(_all_ok(), _NARRATION) == ([], 0, 0)


def test_parse_critic_response_keeps_a_no_with_a_real_quote() -> None:
    raw = _critic_json(single_cta=("curta e se  inscreva no CANAL", "Peça um comentário."))
    findings, unverified, unanswered = parse_critic_response(raw, _NARRATION)
    assert [finding.code for finding in findings] == ["single_cta"]
    assert "Peça um comentário." in findings[0].detail
    assert unverified == 0
    assert unanswered == len(CHECKLIST) - 1


def test_parse_critic_response_drops_a_no_whose_quote_is_not_in_the_script() -> None:
    raw = _critic_json(portuguese=("trecho que não existe", "Corrija."), payoff=True)
    findings, unverified, _unanswered = parse_critic_response(raw, _NARRATION)
    assert findings == []
    assert unverified == 1


def test_parse_critic_response_accepts_absence_without_quote() -> None:
    raw = _critic_json(concrete_example=("", "Mostre uma cena."))
    findings, unverified, _unanswered = parse_critic_response(raw, _NARRATION)
    assert [finding.code for finding in findings] == ["concrete_example"]
    assert unverified == 0


def test_parse_critic_response_ignores_unknown_and_repeated_ids() -> None:
    raw = json.dumps(
        {
            "checks": [
                {"id": "inventado", "ok": False, "quote": "", "fix": ""},
                {"id": "payoff", "ok": True, "quote": "", "fix": ""},
                {"id": "payoff", "ok": False, "quote": "", "fix": ""},
                "lixo",
            ]
        }
    )
    assert parse_critic_response(raw, _NARRATION)[0] == []


def test_parse_critic_response_counts_only_the_claims_the_critic_confirms() -> None:
    claims = ["Reduza a lista para 3 tarefas por dia.", "Estudos mostram que 73% desistem."]
    raw = _critic_json(payoff=True, claim_1=True, claim_2=("", ""))
    findings, _unverified, unanswered = parse_critic_response(raw, _NARRATION, claims)
    assert [finding.code for finding in findings] == ["unsourced_claim"]
    assert "73%" in findings[0].detail
    assert unanswered == len(CHECKLIST) - 1


def test_parse_critic_response_ignores_a_claim_id_that_was_not_asked() -> None:
    raw = _critic_json(payoff=True, claim_3=("", ""))
    assert parse_critic_response(raw, _NARRATION, ["só uma frase com 3 itens"])[0] == []


def test_critic_prompts_and_schema_list_the_flagged_sentences() -> None:
    _system, user = critic_prompts(_TOPIC.topic, _NARRATION, ["Frase com 73%."])
    assert "- claim_1: «Frase com 73%.»" in user
    accepted = critic_schema(1)["properties"]["checks"]["items"]["properties"]["id"]["enum"]
    assert "claim_1" in accepted
    assert "Frases a conferir" not in critic_prompts(_TOPIC.topic, _NARRATION)[1]


def test_review_script_sends_the_heuristic_claims_to_the_critic() -> None:
    narration = _NARRATION + " Estudos mostram que 73% das pessoas desistem."
    ask = MagicMock(return_value=_critic_json(payoff=True, claim_1=("", "")))
    review = review_script(narration, topic=_TOPIC, ask_critic=ask)
    assert ask.call_args.args[2] == ["Estudos mostram que 73% das pessoas desistem."]
    assert [finding.code for finding in review.findings] == ["unsourced_claim"]
    assert review.flagged_claims == 1


@pytest.mark.parametrize(
    "raw",
    ["não é json", "[]", '{"checks": "x"}', '{"checks": []}', '{"checks": [{"id": "x"}]}'],
)
def test_parse_critic_response_rejects_malformed_answers(raw: str) -> None:
    with pytest.raises(CriticResponseError):
        parse_critic_response(raw, _NARRATION)


def test_review_script_retries_a_malformed_answer_once() -> None:
    ask = MagicMock(side_effect=["lixo", _all_ok()])
    review = review_script(_NARRATION, topic=_TOPIC, ask_critic=ask)
    assert review.critic_answered is True
    assert review.findings == ()
    assert ask.call_count == 2


def test_review_script_reports_an_unreadable_critic() -> None:
    ask = MagicMock(return_value="lixo")
    review = review_script(_NARRATION, topic=_TOPIC, ask_critic=ask)
    assert review.critic_answered is False
    assert ask.call_count == 2


# ── Ciclo revisar→reescrever ──────────────────────────────────────────────────


def test_refine_stops_immediately_on_a_clean_script() -> None:
    review, rewrite = _scripted({"v0": _review(0)}, [])
    result = refine("v0", review=review, rewrite=rewrite)
    assert (result.best, result.stop_reason, result.rewrites) == ("v0", "clean", 0)
    rewrite.assert_not_called()


def test_refine_keeps_improving_until_clean() -> None:
    review, rewrite = _scripted(
        {"v0": _review(3), "v1": _review(1), "v2": _review(0)}, ["v1", "v2"]
    )
    result = refine("v0", review=review, rewrite=rewrite)
    assert (result.best, result.stop_reason, result.rewrites) == ("v2", "clean", 2)


def test_refine_keeps_the_previous_version_when_a_rewrite_does_not_improve() -> None:
    review, rewrite = _scripted({"v0": _review(2), "v1": _review(2)}, ["v1"])
    result = refine("v0", review=review, rewrite=rewrite)
    assert (result.best, result.stop_reason) == ("v0", "no_improvement")
    assert result.versions == ("v0", "v1")


def test_refine_respects_the_round_ceiling() -> None:
    review, rewrite = _scripted(
        {"v0": _review(9), "v1": _review(7), "v2": _review(5), "v3": _review(3)},
        ["v1", "v2", "v3", "v4"],
    )
    result = refine("v0", review=review, rewrite=rewrite, max_rounds=3)
    assert (result.best, result.stop_reason, result.rewrites) == ("v3", "max_rounds", 3)
    assert rewrite.call_count == 3


def test_refine_does_nothing_when_the_first_review_has_no_critic() -> None:
    review, rewrite = _scripted({"v0": _review(1, critic_answered=False)}, [])
    result = refine("v0", review=review, rewrite=rewrite)
    assert (result.best, result.stop_reason) == ("v0", "critic_unavailable")
    rewrite.assert_not_called()


def test_refine_discards_a_rewrite_the_critic_could_not_review() -> None:
    review, rewrite = _scripted(
        {"v0": _review(3), "v1": _review(0, critic_answered=False)}, ["v1"]
    )
    result = refine("v0", review=review, rewrite=rewrite)
    assert (result.best, result.stop_reason) == ("v0", "critic_unavailable")


def test_refine_stops_when_the_rewrite_fails() -> None:
    review, rewrite = _scripted({"v0": _review(2)}, [None])
    result = refine("v0", review=review, rewrite=rewrite)
    assert (result.best, result.stop_reason, result.rewrites) == ("v0", "rewrite_failed", 0)


# ── Reescrita ─────────────────────────────────────────────────────────────────


def test_rewrite_script_retries_until_the_length_fits() -> None:
    client = _llm_client(_words(60), _words(120))
    findings = [Finding("critic", "payoff", "O roteiro não entrega o que promete.")]
    assert rewrite_script(client, _TOPIC, _NARRATION, findings) == _words(120)
    second_system = client.chat.completions.create.call_args_list[1].kwargs["messages"][0]
    assert "pelo menos 40 palavras a mais" in second_system["content"]
    first_user = client.chat.completions.create.call_args_list[0].kwargs["messages"][1]
    assert "O roteiro não entrega o que promete." in first_user["content"]


def test_rewrite_script_gives_up_when_the_length_never_fits() -> None:
    client = _llm_client(_words(60), _words(60), _words(60))
    assert rewrite_script(client, _TOPIC, _NARRATION, []) is None


# ── Seleção e planilha ────────────────────────────────────────────────────────


def test_pick_best_prefers_fewer_findings_then_the_first() -> None:
    assert pick_best([_review(2), _review(1), _review(1)]) == 1


def test_pick_best_ranks_a_candidate_without_critic_last() -> None:
    assert pick_best([_review(0, critic_answered=False), _review(4)]) == 1


def test_blind_entries_is_reproducible_and_merges_identical_arms() -> None:
    arms = {"A": "igual", "B": "igual", "C": "outro"}
    first = blind_entries(arms, random.Random(8))
    second = blind_entries(arms, random.Random(8))
    assert [(e.label, e.narration, e.arms) for e in first] == [
        (e.label, e.narration, e.arms) for e in second
    ]
    assert sorted(entry.label for entry in first) == ["A", "B"]
    assert {tuple(entry.arms) for entry in first} == {("A", "B"), ("C",)}


def test_sheet_hides_the_arms_and_the_answer_key_reveals_them() -> None:
    entries = [[Entry("texto do controle", ["A"], "B"), Entry("texto novo", ["B", "C"], "A")]]
    refined = RefineResult(
        versions=("texto do controle", "texto novo"),
        reviews=(_review(2), _review(0)),
        best_index=1,
        stop_reason="clean",
    )
    sheet = render_sheet([_TOPIC], entries)
    assert "### Roteiro 1B" in sheet and "texto do controle" in sheet
    assert "Qual destes você assistiria até o fim?" in sheet
    assert "Braço" not in sheet and "crítico" not in sheet
    key = render_answer_key([_TOPIC], entries, [refined])
    assert "| A | B e C |" in key
    assert "1 reescrita(s), apontamentos 2 → 0, parada por `clean`" in key
