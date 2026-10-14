"""E8 — crítico de outra família de modelo sobre o roteiro do A1.

Experimento fora do pipeline: não cria run, não grava no banco e não altera o A1.
Para cada pauta gera três braços e escreve uma planilha cega para o proprietário:

  A. o roteiro do pipeline atual (controle);
  B. o roteiro A revisado por um crítico de outra família de modelo e reescrito pelo
     roteirista, em no máximo três rodadas;
  C. três roteiros do pipeline atual, dos quais fica o de menos apontamentos, sem
     reescrita.

O crítico responde a uma lista fechada de perguntas de sim/não. O que é contável
(tamanho do gancho, frases longas, estilo de abertura) é conferido em código e não vai
para o modelo. As frases que a heurística do A1 marca como dado sem fonte só contam
quando o crítico confirma que apresentam um dado como fato: a heurística sozinha marca
também dicas e exemplos com números.

Uso, com o Ollama local no ar e nenhuma run em execução:

    python -m scripts.experiments.e8_script_critic --output docs/experiments/e8-critico
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import OpenAI

from src.core.config import settings
from src.pipeline.script_gen import (
    _OPENING_STYLE_RULES,
    OpeningStyle,
    ScriptLengthError,
    SourcedFact,
    Structure,
    _call_llm,
    _count_words,
    _extract_narration,
    _facts_rule,
    _make_client,
    _opening_style_problem,
    _system_base,
    find_unsourced_claims,
    generate_script,
    sanitize_llm_output,
)

DEFAULT_CRITIC_MODEL = "mistral-small3.2:24b"
# Sem contexto fixo o Ollama carrega os modelos maiores com a janela default e estoura a VRAM.
CRITIC_NUM_CTX = 8192
CRITIC_TIMEOUT_SECONDS = 300.0
CRITIC_ATTEMPTS = 2

MAX_ROUNDS = 3
CANDIDATES_ARM_C = 3
GENERATION_ATTEMPTS = 3
REWRITE_LENGTH_ATTEMPTS = 3

WORD_RANGE = (100, 150)
HOOK_MAX_WORDS = 25
SENTENCE_MAX_WORDS = 30

PERSONA = "Coach de produtividade direto e prático, que fala com quem trabalha ou estuda"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_WHITESPACE_RE = re.compile(r"\s+")
_FIX_MAX_CHARS = 200


# ── Tipos ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Topic:
    """Uma pauta do experimento; os três braços recebem exatamente o mesmo pedido."""

    topic: str
    opening_style: OpeningStyle
    structure: Structure = "direct"
    facts: tuple[SourcedFact, ...] = ()


# Pautas sem fatos anexados: a regra do A1 proíbe então qualquer número ou estudo.
DEFAULT_TOPICS: tuple[Topic, ...] = (
    Topic("Por que uma lista de tarefas longa faz você render menos", "contrarian"),
    Topic("O erro de estudar relendo o material em vez de se testar", "mistake"),
    Topic("Como começar uma tarefa que você está adiando há dias", "scene"),
)


@dataclass(frozen=True)
class Check:
    """Uma pergunta de sim/não da lista fechada do crítico."""

    id: str
    question: str
    problem: str
    # Quando True, um "não" só vale se citar um trecho que existe no roteiro.
    needs_quote: bool


CHECKLIST: tuple[Check, ...] = (
    Check(
        "hook_specific",
        "A abertura faz uma promessa ou cria uma tensão específica deste tema, que não "
        "serviria para um vídeo sobre outro assunto?",
        "A abertura é genérica: serviria para qualquer vídeo.",
        needs_quote=False,
    ),
    Check(
        "payoff",
        "O roteiro entrega, antes de terminar, o que a abertura promete?",
        "O roteiro não entrega o que a abertura promete.",
        needs_quote=False,
    ),
    Check(
        "concrete_example",
        "Há pelo menos uma situação concreta do cotidiano que dá para imaginar, e não só "
        "afirmações gerais?",
        "Falta um exemplo concreto do cotidiano.",
        needs_quote=False,
    ),
    Check(
        "no_repetition",
        "Cada parágrafo traz uma ideia nova, sem repetir a abertura nem uma ideia já dita "
        "com outras palavras?",
        "Há uma ideia repetida com outras palavras.",
        needs_quote=True,
    ),
    Check(
        "portuguese",
        "O texto está em português do Brasil correto e natural para ser falado, sem erro "
        "de gramática ou concordância, palavra estrangeira ou frase truncada?",
        "Há um erro de português ou uma frase que não soa natural falada.",
        needs_quote=True,
    ),
    Check(
        "single_cta",
        "O fechamento faz um único pedido ao espectador, ligado ao tema, em vez de "
        "fórmulas genéricas como 'curta e se inscreva'?",
        "O fechamento usa um pedido genérico ou mais de um pedido.",
        needs_quote=True,
    ),
)
_CHECKS_BY_ID = {check.id: check for check in CHECKLIST}


@dataclass(frozen=True)
class Finding:
    """Um problema apontado em um roteiro, pelo código ou pelo crítico."""

    source: Literal["code", "critic"]
    code: str
    detail: str


@dataclass(frozen=True)
class Review:
    """Resultado da revisão de uma versão do roteiro."""

    findings: tuple[Finding, ...]
    # False quando a resposta do crítico não pôde ser lida; só os achados do código valem.
    critic_answered: bool = True
    # "Não" do crítico descartado por citar um trecho que não está no roteiro.
    unverified: int = 0
    unanswered: int = 0
    # Frases marcadas pela heurística de dados sem fonte e enviadas ao crítico.
    flagged_claims: int = 0
    seconds: float = 0.0


StopReason = Literal[
    "clean", "no_improvement", "max_rounds", "critic_unavailable", "rewrite_failed"
]


@dataclass(frozen=True)
class RefineResult:
    """Histórico do ciclo revisar→reescrever do braço B."""

    versions: tuple[str, ...]
    reviews: tuple[Review, ...]
    best_index: int
    stop_reason: StopReason

    @property
    def best(self) -> str:
        return self.versions[self.best_index]

    @property
    def rewrites(self) -> int:
        return len(self.versions) - 1


class CriticResponseError(ValueError):
    """A resposta do crítico não é o JSON pedido ou não responde a nenhuma pergunta."""


# ── Checagens em código ───────────────────────────────────────────────────────


def split_hook(narration: str) -> str:
    """A abertura é o primeiro parágrafo, como no roteiro montado pelo A1."""
    return narration.strip().split("\n\n", 1)[0].strip()


def code_findings(narration: str, *, opening_style: OpeningStyle | None) -> list[Finding]:
    """Problemas contáveis, conferidos sem modelo."""
    findings: list[Finding] = []
    hook = split_hook(narration)
    hook_words = _count_words(hook)
    if hook_words > HOOK_MAX_WORDS:
        findings.append(
            Finding(
                "code",
                "hook_too_long",
                f"A abertura tem {hook_words} palavras; o máximo é {HOOK_MAX_WORDS}.",
            )
        )
    if opening_style is not None:
        problem = _opening_style_problem(hook, opening_style)
        if problem is not None:
            findings.append(
                Finding("code", "opening_style", f"A abertura {problem}, contra o estilo pedido.")
            )
    for raw in _SENTENCE_SPLIT_RE.split(narration):
        sentence = raw.strip()
        words = _count_words(sentence)
        if words > SENTENCE_MAX_WORDS:
            findings.append(
                Finding(
                    "code",
                    "long_sentence",
                    f"Frase com {words} palavras, longa demais para narrar; divida-a: "
                    f"«{sentence}»",
                )
            )
    return findings


def in_word_range(narration: str) -> bool:
    return WORD_RANGE[0] <= _count_words(narration) <= WORD_RANGE[1]


# ── Crítico ───────────────────────────────────────────────────────────────────


_CLAIM_ID_PREFIX = "claim_"
_CLAIM_QUESTION = (
    "Para cada frase abaixo, responda ok=false SOMENTE se ela apresenta como fato um "
    "número, uma estatística, uma proporção ou um estudo. Quantidade sugerida numa dica, "
    "duração de um exercício, contagem de itens e exemplo hipotético não são dados: "
    "nesses casos responda ok=true."
)


def critic_prompts(topic: str, narration: str, claims: Sequence[str] = ()) -> tuple[str, str]:
    """Prompts do crítico: responde só à lista fechada, sem nota de preferência."""
    system = (
        "Você é um revisor de roteiros de vídeos curtos em português do Brasil. "
        "Você não reescreve o roteiro e não dá nota: responde a uma lista de perguntas "
        "de sim ou não. Seja exigente; na dúvida, responda que não.\n"
        "Responda APENAS com JSON no formato "
        '{"checks": [{"id": "...", "ok": true, "quote": "", "fix": ""}]}, '
        "com um item para cada pergunta.\n"
        "- ok: true se a resposta à pergunta é sim, false se é não.\n"
        "- quote: quando ok é false, o trecho exato do roteiro que mostra o problema, "
        "copiado sem alterar nenhuma letra; vazio quando o problema é uma ausência.\n"
        "- fix: quando ok é false, uma instrução curta de como corrigir; senão, vazio."
    )
    questions = "\n".join(f"- {check.id}: {check.question}" for check in CHECKLIST)
    user = (
        f"Tema do vídeo: {topic}\n\n"
        f"Abertura (primeiro parágrafo):\n{split_hook(narration)}\n\n"
        f"Roteiro completo:\n{narration}\n\n"
        f"Perguntas:\n{questions}"
    )
    if claims:
        listed = "\n".join(
            f"- {_CLAIM_ID_PREFIX}{index}: «{claim}»" for index, claim in enumerate(claims, 1)
        )
        user += f"\n\nFrases a conferir. {_CLAIM_QUESTION}\n{listed}"
    return system, user


def _normalized(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def parse_critic_response(
    raw: str, narration: str, claims: Sequence[str] = ()
) -> tuple[list[Finding], int, int]:
    """
    Converte a resposta do crítico em achados.

    Devolve (achados, descartados, sem_resposta). Um "não" que exige citação é
    descartado quando o trecho citado não existe no roteiro: é a defesa contra o
    crítico apontar um problema que não está no texto. Uma frase marcada pela
    heurística de dados sem fonte só vira achado quando o crítico a confirma.
    """
    try:
        payload = json.loads(sanitize_llm_output(raw))
    except json.JSONDecodeError as exc:
        raise CriticResponseError("resposta do crítico não é JSON") from exc
    items = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise CriticResponseError("resposta do crítico não traz a lista 'checks'")

    haystack = _normalized(narration)
    claims_by_id = {
        f"{_CLAIM_ID_PREFIX}{index}": claim for index, claim in enumerate(claims, 1)
    }
    answered: set[str] = set()
    findings: list[Finding] = []
    unverified = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id"))
        ok = item.get("ok")
        if not isinstance(ok, bool) or item_id in answered:
            continue
        claim = claims_by_id.get(item_id)
        if claim is not None:
            answered.add(item_id)
            if not ok:
                findings.append(
                    Finding(
                        "critic",
                        "unsourced_claim",
                        "A frase apresenta como fato um dado sem fonte; reescreva-a sem o "
                        f"número ou o estudo: «{claim}»",
                    )
                )
            continue
        check = _CHECKS_BY_ID.get(item_id)
        if check is None:
            continue
        answered.add(item_id)
        if ok:
            continue
        quote = item.get("quote")
        quote = quote.strip() if isinstance(quote, str) else ""
        if check.needs_quote and (not quote or _normalized(quote) not in haystack):
            unverified += 1
            continue
        detail = check.problem
        if quote and _normalized(quote) in haystack:
            detail += f" Trecho: «{quote}»."
        fix = item.get("fix")
        if isinstance(fix, str) and fix.strip():
            detail += f" Sugestão do revisor: {fix.strip()[:_FIX_MAX_CHARS]}"
        findings.append(Finding("critic", check.id, detail))
    if not answered:
        raise CriticResponseError("resposta do crítico não responde a nenhuma pergunta")
    return findings, unverified, len(CHECKLIST) + len(claims) - len(answered)


def critic_schema(claim_count: int) -> dict[str, Any]:
    """Formato exigido da resposta; os ids aceitos são os da lista e os das frases."""
    ids = [check.id for check in CHECKLIST]
    ids += [f"{_CLAIM_ID_PREFIX}{index}" for index in range(1, claim_count + 1)]
    return {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "ok": {"type": "boolean"},
                        "quote": {"type": "string"},
                        "fix": {"type": "string"},
                    },
                    "required": ["id", "ok", "quote", "fix"],
                },
            }
        },
        "required": ["checks"],
    }


def native_api_root() -> str:
    """Raiz da API nativa do Ollama, a partir da raiz OpenAI-compatível usada pelo A1."""
    return settings.ollama_base_url.rstrip("/").removesuffix("/v1")


class OllamaCritic:
    """Crítico pela API nativa do Ollama, a única que aceita fixar a janela de contexto."""

    def __init__(self, http: httpx.Client, *, model: str) -> None:
        self._http = http
        self.model = model
        # Modelos carregados logo depois da primeira resposta: registra contexto e VRAM.
        self.loaded_after_first_call: Any = None

    def ask(self, topic: str, narration: str, claims: Sequence[str]) -> str:
        system, user = critic_prompts(topic, narration, claims)
        response = self._http.post(
            f"{native_api_root()}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": critic_schema(len(claims)),
                "options": {"num_ctx": CRITIC_NUM_CTX, "temperature": 0},
            },
        )
        response.raise_for_status()
        if self.loaded_after_first_call is None:
            self.loaded_after_first_call = (
                self._http.get(f"{native_api_root()}/api/ps").json().get("models")
            )
        return str(response.json()["message"]["content"])


def review_script(
    narration: str,
    *,
    topic: Topic,
    ask_critic: Callable[[str, str, Sequence[str]], str],
) -> Review:
    """Revisão completa de uma versão: checagens em código mais a lista do crítico."""
    findings = code_findings(narration, opening_style=topic.opening_style)
    claims = find_unsourced_claims(narration, topic=topic.topic, facts=topic.facts)
    started = time.monotonic()
    for _attempt in range(CRITIC_ATTEMPTS):
        try:
            critic, unverified, unanswered = parse_critic_response(
                ask_critic(topic.topic, narration, claims), narration, claims
            )
        except CriticResponseError:
            continue
        return Review(
            findings=(*findings, *critic),
            unverified=unverified,
            unanswered=unanswered,
            flagged_claims=len(claims),
            seconds=round(time.monotonic() - started, 1),
        )
    return Review(
        findings=tuple(findings),
        critic_answered=False,
        flagged_claims=len(claims),
        seconds=round(time.monotonic() - started, 1),
    )


# ── Reescrita ─────────────────────────────────────────────────────────────────


def rewrite_prompts(
    topic: Topic,
    narration: str,
    findings: Sequence[Finding],
    *,
    previous_words: int | None = None,
) -> tuple[str, str]:
    """Prompts do roteirista para corrigir só o que foi apontado."""
    style_block = ""
    if topic.opening_style is not None:
        style_block = f" Estilo da abertura: {_OPENING_STYLE_RULES[topic.opening_style]}"
    # Corrigir cortando encurta o texto: o modelo precisa saber o tamanho de partida e,
    # numa nova tentativa, quanto falta ou sobra.
    length_block = (
        f" O roteiro atual tem {_count_words(narration)} palavras: ao tirar um trecho, "
        "ponha outro no lugar."
    )
    if previous_words is not None:
        if previous_words < WORD_RANGE[0]:
            correction = (
                f"curta demais: escreva pelo menos {WORD_RANGE[0] - previous_words} "
                "palavras a mais"
            )
        else:
            correction = (
                f"longa demais: corte pelo menos {previous_words - WORD_RANGE[1]} palavras"
            )
        length_block += (
            f" A tentativa anterior ficou com {previous_words} palavras e foi recusada "
            f"por estar {correction}."
        )
    system = (
        f"{_system_base(PERSONA, 'short')}\n\n"
        "Sua tarefa: REESCREVER o roteiro corrigindo SOMENTE os problemas listados. "
        "Mantenha o tema, o ângulo, o tom e tudo o que não foi apontado. "
        "O primeiro parágrafo é a abertura do vídeo: mantenha-a como um parágrafo "
        f"separado, com no máximo {HOOK_MAX_WORDS} palavras.{style_block} "
        f"TAMANHO OBRIGATÓRIO: o roteiro inteiro deve ter entre {WORD_RANGE[0]} e "
        f"{WORD_RANGE[1]} palavras faladas.{length_block} "
        "Responda APENAS com o texto falado — sem rótulos, comentários, emojis, "
        "contagem de palavras ou aspas em volta do texto."
        f"{_facts_rule(topic.facts)}"
    )
    problems = "\n".join(f"- {finding.detail}" for finding in findings)
    user = (
        f"Tema: {topic.topic}\n\n"
        f"Roteiro atual:\n{narration}\n\n"
        f"Problemas a corrigir:\n{problems}\n\n"
        "Reescreva o roteiro."
    )
    return system, user


def rewrite_script(
    client: OpenAI,
    topic: Topic,
    narration: str,
    findings: Sequence[Finding],
) -> str | None:
    """Reescreve com o modelo do pipeline; None se o tamanho não entrar na faixa."""
    previous_words: int | None = None
    for _attempt in range(REWRITE_LENGTH_ATTEMPTS):
        system, user = rewrite_prompts(
            topic, narration, findings, previous_words=previous_words
        )
        rewritten = _extract_narration(
            _call_llm(client, system=system, user=user, step="e8_rewrite")
        ).strip("\"“”")
        if in_word_range(rewritten):
            return rewritten
        previous_words = _count_words(rewritten)
    return None


# ── Ciclo e seleção ───────────────────────────────────────────────────────────


def refine(
    initial: str,
    *,
    review: Callable[[str], Review],
    rewrite: Callable[[str, Sequence[Finding]], str | None],
    max_rounds: int = MAX_ROUNDS,
) -> RefineResult:
    """
    Ciclo revisar→reescrever com teto de rodadas.

    Para quando a versão atual não tem apontamentos, quando a reescrita não reduz a
    quantidade de apontamentos, quando o crítico ou a reescrita falham, ou no teto.
    Fica a versão com menos apontamentos; em caso de empate, a mais antiga.
    """
    versions = [initial]
    reviews = [review(initial)]
    best = 0

    def result(reason: StopReason) -> RefineResult:
        return RefineResult(tuple(versions), tuple(reviews), best, reason)

    if not reviews[0].critic_answered:
        return result("critic_unavailable")
    for _round in range(max_rounds):
        if not reviews[best].findings:
            return result("clean")
        rewritten = rewrite(versions[best], reviews[best].findings)
        if rewritten is None:
            return result("rewrite_failed")
        candidate_review = review(rewritten)
        versions.append(rewritten)
        reviews.append(candidate_review)
        if not candidate_review.critic_answered:
            return result("critic_unavailable")
        if len(candidate_review.findings) >= len(reviews[best].findings):
            return result("no_improvement")
        best = len(versions) - 1
    return result("clean" if not reviews[best].findings else "max_rounds")


def pick_best(reviews: Sequence[Review]) -> int:
    """Índice do candidato com menos apontamentos; sem crítico fica por último."""
    return min(
        range(len(reviews)),
        key=lambda index: (
            not reviews[index].critic_answered,
            len(reviews[index].findings),
            index,
        ),
    )


# ── Planilha cega ─────────────────────────────────────────────────────────────


@dataclass
class Entry:
    """Um roteiro da planilha; braços com o mesmo texto viram uma única entrada."""

    narration: str
    arms: list[str] = field(default_factory=list)
    label: str = ""


def blind_entries(arm_texts: dict[str, str], rng: random.Random) -> list[Entry]:
    """Agrupa braços de texto idêntico, embaralha e atribui as letras da planilha."""
    by_text: dict[str, Entry] = {}
    for arm, narration in arm_texts.items():
        by_text.setdefault(narration, Entry(narration)).arms.append(arm)
    entries = list(by_text.values())
    rng.shuffle(entries)
    for index, entry in enumerate(entries):
        entry.label = chr(ord("A") + index)
    return entries


def render_sheet(topics: Sequence[Topic], entries: Sequence[Sequence[Entry]]) -> str:
    lines = [
        "# E8 — avaliação cega de roteiros",
        "",
        "Cada pauta tem roteiros produzidos por processos diferentes, em ordem embaralhada.",
        "Dê uma nota de 1 a 5 em cada coluna sem abrir o gabarito e, no fim de cada pauta,",
        "escreva a letra do roteiro que você assistiria até o fim.",
        "",
        "- **Fatos:** 5 = nenhum número ou estudo inventado; 1 = vários.",
        "- **Gramática:** 5 = português natural e correto; 1 = erros que você ouviria na narração.",
        "- **Gancho:** 5 = a primeira frase prende; 1 = genérica.",
        "- **Ritmo:** 5 = flui e fecha bem em voz alta; 1 = arrastado ou picotado.",
        "",
    ]
    for number, (topic, topic_entries) in enumerate(zip(topics, entries, strict=True), 1):
        lines += [f"## Pauta {number}: {topic.topic}", ""]
        for entry in topic_entries:
            lines += [
                f"### Roteiro {number}{entry.label}",
                "",
                entry.narration,
                "",
                "| Fatos | Gramática | Gancho | Ritmo |",
                "|---|---|---|---|",
                "|  |  |  |  |",
                "",
            ]
        lines += ["**Qual destes você assistiria até o fim?** ", "", "---", ""]
    return "\n".join(lines)


def render_answer_key(
    topics: Sequence[Topic],
    entries: Sequence[Sequence[Entry]],
    refinements: Sequence[RefineResult],
) -> str:
    lines = ["# E8 — gabarito (abra só depois de dar as notas)", ""]
    lines += [
        "- **A:** pipeline atual.",
        "- **B:** A revisado pelo crítico e reescrito.",
        "- **C:** o melhor de três roteiros do pipeline atual, escolhido pelo crítico.",
        "",
    ]
    for number, (topic_entries, refined) in enumerate(zip(entries, refinements, strict=True), 1):
        before = len(refined.reviews[0].findings)
        after = len(refined.reviews[refined.best_index].findings)
        lines += [
            f"## Pauta {number}: {topics[number - 1].topic}",
            "",
            "| Roteiro | Braço | Palavras | Abertura (palavras) |",
            "|---|---|---|---|",
        ]
        for entry in topic_entries:
            lines.append(
                f"| {entry.label} | {' e '.join(sorted(entry.arms))} "
                f"| {_count_words(entry.narration)} "
                f"| {_count_words(split_hook(entry.narration))} |"
            )
        lines += [
            "",
            f"Braço B: {refined.rewrites} reescrita(s), apontamentos {before} → {after}, "
            f"parada por `{refined.stop_reason}`.",
            "",
        ]
    return "\n".join(lines)


# ── Execução ──────────────────────────────────────────────────────────────────


def _generate(client: OpenAI, topic: Topic) -> tuple[str, float]:
    """Um roteiro do pipeline atual; a falha de tamanho do A1 é refeita como numa nova run."""
    started = time.monotonic()
    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        try:
            result = generate_script(
                topic.topic,
                PERSONA,
                "short",
                facts=topic.facts,
                opening_style=topic.opening_style,
                structure=topic.structure,
                client=client,
            )
        except ScriptLengthError:
            if attempt == GENERATION_ATTEMPTS:
                raise
            continue
        return result.narration, round(time.monotonic() - started, 1)
    raise AssertionError("unreachable")


def _review_data(review: Review) -> dict[str, Any]:
    return asdict(review)


def run(output: Path, *, critic_model: str, seed: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    sheet_path = output / "avaliacao-cega.md"
    if sheet_path.exists():
        # A planilha pode já estar preenchida com as notas do proprietário.
        raise SystemExit(f"{sheet_path} já existe; escolha outra pasta em --output")

    topics = DEFAULT_TOPICS
    rng = random.Random(seed)
    client = _make_client()
    all_entries: list[list[Entry]] = []
    refinements: list[RefineResult] = []
    records: list[dict[str, Any]] = []

    with httpx.Client(timeout=CRITIC_TIMEOUT_SECONDS) as http:
        critic = OllamaCritic(http, model=critic_model)
        for number, topic in enumerate(topics, 1):
            print(f"[pauta {number}] gerando o controle e {CANDIDATES_ARM_C} candidatos")
            control, control_seconds = _generate(client, topic)
            candidates = [_generate(client, topic) for _ in range(CANDIDATES_ARM_C)]

            def review(narration: str, topic: Topic = topic) -> Review:
                return review_script(narration, topic=topic, ask_critic=critic.ask)

            def rewrite(
                narration: str, findings: Sequence[Finding], topic: Topic = topic
            ) -> str | None:
                return rewrite_script(client, topic, narration, findings)

            print(f"[pauta {number}] braço B: revisão e reescrita")
            refined = refine(control, review=review, rewrite=rewrite)

            print(f"[pauta {number}] braço C: revisão dos candidatos")
            candidate_reviews = [review(text) for text, _seconds in candidates]
            chosen = pick_best(candidate_reviews)

            entries = blind_entries(
                {"A": control, "B": refined.best, "C": candidates[chosen][0]}, rng
            )
            all_entries.append(entries)
            refinements.append(refined)
            records.append(
                {
                    "topic": topic.topic,
                    "opening_style": topic.opening_style,
                    "structure": topic.structure,
                    "labels": {entry.label: sorted(entry.arms) for entry in entries},
                    "arm_a": {"narration": control, "seconds": control_seconds},
                    "arm_b": {
                        "stop_reason": refined.stop_reason,
                        "best_index": refined.best_index,
                        "versions": [
                            {"narration": text, "review": _review_data(version_review)}
                            for text, version_review in zip(
                                refined.versions, refined.reviews, strict=True
                            )
                        ],
                    },
                    "arm_c": {
                        "chosen": chosen,
                        "candidates": [
                            {
                                "narration": text,
                                "seconds": seconds,
                                "review": _review_data(candidate_review),
                            }
                            for (text, seconds), candidate_review in zip(
                                candidates, candidate_reviews, strict=True
                            )
                        ],
                    },
                }
            )

    data = {
        "experiment": "e8-script-critic",
        "seed": seed,
        "writer_model": settings.ollama_model,
        "critic_model": critic_model,
        "critic_num_ctx": CRITIC_NUM_CTX,
        "max_rounds": MAX_ROUNDS,
        "persona": PERSONA,
        "checklist": [asdict(check) for check in CHECKLIST],
        "loaded_after_first_review": critic.loaded_after_first_call,
        "topics": records,
    }
    (output / "dados.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "gabarito.md").write_text(
        render_answer_key(topics, all_entries, refinements), encoding="utf-8"
    )
    sheet_path.write_text(render_sheet(topics, all_entries), encoding="utf-8")
    print(f"planilha cega: {sheet_path.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--critic-model", default=DEFAULT_CRITIC_MODEL)
    parser.add_argument("--seed", type=int, default=8)
    arguments = parser.parse_args()
    run(arguments.output, critic_model=arguments.critic_model, seed=arguments.seed)


if __name__ == "__main__":
    main()
