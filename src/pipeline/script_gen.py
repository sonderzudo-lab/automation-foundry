"""
A1 — Geração de roteiro (ARQUITETURA.md Seção A1).

Pipeline de 4 etapas encadeadas via Ollama (cliente OpenAI-compatível):
  1. Ângulo editorial específico
  2. Outline estruturado
  3. Hook — a abertura que será narrada
  4. Restante do roteiro, que continua a partir do hook

O roteiro final é montado no código como hook + restante, de modo que o hook
revisado no gate A1 é exatamente a abertura que chega ao TTS.

Cada etapa usa retry exponencial em caso de timeout, falha de conexão
ou resposta vazia do Ollama.

Duas camadas de defesa contra thinking-mode do Qwen3:
  CAMADA 1 (na raiz): extra_body={"think": False} + /no_think no system prompt.
  CAMADA 2 (defensiva): sanitize_llm_output() aplicada em TODA saída de LLM,
    remove qualquer artefato de raciocínio mesmo que a camada 1 falhe.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import structlog
from openai import APIConnectionError, APITimeoutError, OpenAI
from pydantic import BaseModel, Field
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.core.config import settings

log = structlog.get_logger(__name__)
_std_log = logging.getLogger(__name__)


# ── Tipos ─────────────────────────────────────────────────────────────────────


class ScriptResult(BaseModel):
    """Saída completa da geração de roteiro em 4 etapas."""

    angle: str        # Ângulo editorial específico e diferenciado
    outline: str      # Estrutura/outline do vídeo
    full_script: str  # Roteiro completo com formatação do modelo (para revisão humana)
    hook: str         # Abertura narrada: o início de full_script, já sem formatação
    narration: str    # Texto puro para TTS: sem emojis, markdown, stage directions
    # Frases da narração com número, proporção ou apelo a estudo que a pauta não sustenta.
    # None significa que a verificação não rodou, como nos bundles anteriores a ela.
    unsourced_claims: list[str] | None = None
    # Estilo de abertura e estrutura pedidos para esta geração; None quando não houve pedido.
    opening_style: str | None = None
    # False quando o hook final não seguiu o estilo mesmo depois das novas tentativas.
    opening_style_met: bool | None = None
    structure: str | None = None


class SourcedFact(BaseModel):
    """Fato fornecido pelo operador na pauta, com a fonte que o sustenta."""

    claim: str = Field(min_length=3, max_length=500)
    source: str = Field(min_length=3, max_length=500)


class EmptyResponseError(RuntimeError):
    """Lançada quando o Ollama retorna resposta vazia ou só whitespace."""


class ScriptLengthError(RuntimeError):
    """Lançada quando a narração segue fora da faixa de palavras após todas as tentativas."""


# Faixa de palavras faladas aceita por formato, a mesma prometida em _FORMAT_CONTEXT.
# O formato long ainda não atravessou uma run real e segue sem faixa verificada.
_NARRATION_WORD_RANGE: dict[str, tuple[int, int]] = {"short": (100, 150)}
_LENGTH_ATTEMPTS = 3


# ── Variedade: estilo de abertura e estrutura ─────────────────────────────────

OpeningStyle = Literal["question", "contrarian", "scene", "mistake", "promise"]
Structure = Literal["direct", "myth_fact", "list", "story"]

# A ordem define a rotação automática feita pelo executor.
OPENING_STYLES: tuple[OpeningStyle, ...] = (
    "contrarian",
    "scene",
    "mistake",
    "promise",
    "question",
)
OPENING_STYLE_LABELS: dict[OpeningStyle, str] = {
    "question": "Pergunta ao espectador",
    "contrarian": "Afirmação contra o senso comum",
    "scene": "Cena do cotidiano",
    "mistake": "Erro comum",
    "promise": "Promessa do vídeo",
}
STRUCTURE_LABELS: dict[Structure, str] = {
    "direct": "Direto: gancho, desenvolvimento e fecho",
    "myth_fact": "Mito vs. fato",
    "list": "Lista de itens",
    "story": "História curta",
}

_OPENING_STYLE_RULES: dict[OpeningStyle, str] = {
    "question": (
        "Abra com uma pergunta direta ao espectador sobre uma situação que ele reconhece."
    ),
    "contrarian": (
        "Abra com uma afirmação que contraria uma crença comum sobre o tema. "
        "Não faça pergunta."
    ),
    "scene": (
        "Abra descrevendo uma cena concreta e curta do cotidiano, como se a câmera já "
        "estivesse nela. Não faça pergunta e não comece com 'Você'."
    ),
    "mistake": (
        "Abra apontando um erro comum que as pessoas cometem sobre o tema e sinalize "
        "que existe um jeito melhor. Não faça pergunta."
    ),
    "promise": (
        "Abra dizendo o que o espectador vai conseguir fazer ao final do vídeo, sem "
        "exagero e sem números. Não faça pergunta."
    ),
}
_OPENING_ATTEMPTS = 3

_STRUCTURE_OUTLINE: dict[Structure, dict[str, str]] = {
    "direct": {
        "short": "Crie 3 blocos curtos: gancho, desenvolvimento rápido e call-to-action.",
        "long": "Crie 3–5 seções com títulos e 2–3 pontos-chave por seção.",
    },
    "myth_fact": {
        "short": (
            "Crie 3 blocos curtos: o mito em que muita gente acredita, por que ele engana "
            "e o fato, com uma ação prática."
        ),
        "long": (
            "Crie 3–5 seções, cada uma com um mito comum, por que ele engana e o fato "
            "correspondente; feche com uma ação prática."
        ),
    },
    "list": {
        "short": "Crie exatamente 3 itens curtos e paralelos, um por bloco, e um fecho.",
        "long": (
            "Crie uma lista de 5 itens, um por seção, com 2–3 pontos-chave cada, "
            "e uma conclusão."
        ),
    },
    "story": {
        "short": (
            "Crie 3 blocos curtos de uma história ilustrativa: personagem e situação, "
            "a virada e a lição prática."
        ),
        "long": (
            "Crie 3–5 seções de uma história ilustrativa: personagem e situação, "
            "complicação, virada e lições práticas."
        ),
    },
}
_STRUCTURE_BODY: dict[Structure, str] = {
    "direct": "",
    "myth_fact": " Deixe claro na fala o que é o mito e o que é o fato.",
    "list": " Anuncie a lista e marque cada item na fala (primeiro, segundo, terceiro).",
    "story": (
        " A história é ilustrativa: use um personagem genérico, nunca uma pessoa real, "
        "e não a apresente como caso documentado."
    ),
}
# Quantidade de itens que a própria estrutura pede; não é um dado sem fonte.
_STRUCTURE_SUPPORTED_NUMBERS: dict[Structure, dict[str, str]] = {
    "list": {"short": "3 três", "long": "5 cinco"},
}


def _opening_style_problem(hook: str, style: OpeningStyle) -> str | None:
    """Motivo determinístico pelo qual o hook não seguiu o estilo, ou None se seguiu."""
    if style == "question":
        return None if "?" in hook else "não faz uma pergunta"
    if "?" in hook:
        return "contém uma pergunta"
    if style == "scene" and hook.casefold().startswith("você"):
        return "começa com 'Você'"
    return None


def _opening_style_met(hook: str, style: OpeningStyle) -> bool:
    """Verificação determinística mínima de que o hook seguiu o estilo pedido."""
    return _opening_style_problem(hook, style) is None


# ── Constante de idioma ───────────────────────────────────────────────────────

# Bloco adicionado ao final de TODOS os system prompts para prevenir misturas de idioma.
_LANGUAGE_RULE = (
    "\n\nIDIOMA – REGRA ABSOLUTA:\n"
    "Escreva EXCLUSIVAMENTE em português do Brasil (PT-BR). "
    "NUNCA use palavras de espanhol, inglês ou qualquer outro idioma.\n"
    "Palavras espanholas comuns que DEVEM ser substituídas:\n"
    "  'mientras'  → 'enquanto'\n"
    "  'pero'      → 'mas' / 'porém'\n"
    "  'también'   → 'também'\n"
    "  'muy'       → 'muito'\n"
    "  'hay'       → 'há'\n"
    "  'porque' no sentido espanhol → use 'porque' ou 'pois' em PT-BR\n"
    "  'como si'   → 'como se'\n"
    "  'sin'       → 'sem'\n"
    "  'este/esta' (espanhol) → 'este/esta' em PT-BR é correto, mas confirme o contexto.\n"
    "Antes de responder, revise mentalmente cada palavra e confirme que é português brasileiro."
)


# ── Regex para limpeza de texto ───────────────────────────────────────────────

# Blocos <think>…</think> COMPLETOS (com tag de abertura presente)
_THINK_COMPLETE_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Compatibilidade: alias mantido para testes que ainda referenciam _THINK_RE
_THINK_RE = _THINK_COMPLETE_RE

# Emojis — cobre os principais blocos Unicode
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001F2FF"   # Peças de jogo, letras em caixa e indicadores regionais (bandeiras)
    "\U0001F300-\U0001F9FF"   # Emoticons, misc symbols, transport, flags, etc.
    "\U0001FA00-\U0001FAFF"   # Símbolos e pictogramas recentes
    "\U000E0020-\U000E007F"   # Tags invisíveis de bandeiras regionais
    "\U0000FE00-\U0000FE0F"   # Seletores de variação (o U+FE0F que sobra de "⚠️")
    "\U0000200D"              # Zero-width joiner de sequências compostas
    "\U000020E3"              # Keycap combinante de "1️⃣"
    "\U00002600-\U000026FF"   # Símbolos miscelâneos (☀️☔etc)
    "\U00002700-\U000027BF"   # Dingbats
    "\U00002B00-\U00002BFF"   # Símbolos e setas miscelâneos
    "\U00002300-\U000023FF"   # Técnicos miscelâneos
    "]+",
    re.UNICODE,
)

# Linhas horizontais (---, ***, ___)
_HR_RE = re.compile(r"^\s*(?:[-*_]){3,}\s*$", re.MULTILINE)

# Linha que é um cabeçalho Markdown: # texto, ## texto, etc.
_MD_HEADER_LINE_RE = re.compile(r"^\s*#{1,6}\s")

# Anotações de timing em rótulos de seção: (0-5s), (5–15s), (10s)
_SECTION_TIMING_RE = re.compile(r"\(\s*\d+\s*[-–]?\s*\d*\s*s\s*\)")

# Bold/italic Markdown: **texto**, ***texto***
_MD_BOLD_RE = re.compile(r"\*{2,3}([^*\n]*)\*{2,3}")

# Sublinhado Markdown: __texto__
_MD_UNDERLINE_RE = re.compile(r"_{2}([^_\n]*)_{2}")

# Direções de cena inline (single asterisk): *pausa*, *clique de relógio*
# Captura *texto* onde texto tem ≤ 80 chars e não contém outro * (distingue de **bold**)
_STAGE_DIRECTION_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]{1,80})(?<!\*)\*(?!\*)")

# Anotações de tempo nas linhas de narração: (0-5s), (00:30), [00:30], (Gancho – 5s)
_TIMING_ANNOTATION_RE = re.compile(
    r"\([^)]{0,25}(?:\d+s|\d+:\d{2})[^)]{0,25}\)"
    r"|\[\d+:\d{2}\]",
    re.UNICODE,
)

# Rótulos entre colchetes ([10-40 segundos], [Desenvolvimento]) e a contagem que o
# modelo anota ao receber uma faixa de tamanho: (100 palavras), (Total: ~120 palavras).
# Hashtags no fim do roteiro: sem isto o "#" some e as palavras são narradas.
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
_BRACKET_LABEL_RE = re.compile(r"\[[^\]\n]{0,60}\]")
_WORD_COUNT_NOTE_RE = re.compile(r"\([^)\n]{0,20}\d+\s+palavras[^)\n]{0,20}\)", re.IGNORECASE)

# Marcadores de lista no início da linha. A voz lê "1." como "um" e a legenda, que
# copia a grafia da narração, mostra "1." sozinho na tela (run 17).
_NUMBERED_MARKER_RE = re.compile(r"^(\d{1,2})[.)]\s+(?=\S)")
_BULLET_MARKER_RE = re.compile(r"^[-•]\s+(?=\S)")
_SPOKEN_ORDINALS = (
    "Primeiro", "Segundo", "Terceiro", "Quarto", "Quinto",
    "Sexto", "Sétimo", "Oitavo", "Nono", "Décimo",
)
_FIRST_WORD_RE = re.compile(r"^(\w+)(?=\W|$)")

# "Ex:" e "Ex.:" — a transcrição não ouve a abreviação e o trecho fica sem legenda.
_EXAMPLE_ABBREVIATION_RE = re.compile(r"(?<!\w)([Ee])x\.?:(?=\s)")

# Aspas: as que fecham ou abrem sem par na mesma linha são removidas (na run 17 o
# modelo abriu aspas num parágrafo e fechou noutro). Apóstrofo entre letras fica.
_OPENING_QUOTES = {"“": "”", "‘": "’", "«": "»"}
_CLOSING_QUOTES = {closing: opening for opening, closing in _OPENING_QUOTES.items()}
_SYMMETRIC_QUOTES = frozenset("\"'")


# ── Funções de limpeza ────────────────────────────────────────────────────────


def sanitize_llm_output(text: str) -> str:
    """
    Remove TODOS os artefatos de raciocínio interno (thinking mode) da saída do LLM.

    É a CAMADA 2 de defesa (defensiva), aplicada em todas as 4 etapas
    independentemente de a Camada 1 (extra_body + /no_think) ter funcionado.

    Trata os três padrões reais observados no Qwen3 via Ollama:

    Padrão A — bloco completo (tags de abertura E fechamento presentes):
        "<think>raciocínio em inglês...</think>\\n\\nConteúdo em PT-BR."
        → "Conteúdo em PT-BR."

    Padrão B — tag de abertura AUSENTE (padrão mais comum no Ollama):
        "nowrap\\n\\nOkay, the user wants a script about...\\n</think>\\n\\nConteúdo."
        → "Conteúdo."
        O "nowrap" e tudo que vem antes de </think> é raciocínio vazado.

    Padrão C — texto limpo sem nenhum artefato:
        "Conteúdo direto em PT-BR."
        → "Conteúdo direto em PT-BR."  (inalterado)

    Args:
        text: Saída bruta do LLM.

    Returns:
        Texto limpo, sem artefatos de raciocínio, com whitespace normalizado.
    """
    # Passo 1: Remove blocos <think>…</think> completos (Padrão A).
    # Usa re.DOTALL para capturar raciocínio multilinha.
    cleaned = _THINK_COMPLETE_RE.sub("", text)

    # Passo 2: Trata </think> ÓRFÃO — tudo antes (e incluindo) o último </think>
    # é raciocínio que vazou sem tag de abertura (Padrão B).
    closing = "</think>"
    lower = cleaned.lower()
    if closing in lower:
        last_idx = lower.rfind(closing)
        cleaned = cleaned[last_idx + len(closing):]

    # Passo 3: Remove o token "nowrap" que o Qwen3 emite como prefixo de raciocínio.
    # Pode restar após o passo 2 se o think foi totalmente órfão sem </think>.
    cleaned = re.sub(r"^\s*nowrap\b[^\n]*\n?", "", cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


# Alias interno mantido para compatibilidade com _extract_narration
_strip_think_blocks = sanitize_llm_output


def _normalize_markdown(text: str) -> str:
    """
    Remove toda formatação markdown de uma string, mantendo apenas o conteúdo textual.
    Usado internamente para determinar se uma linha é apenas um rótulo de seção.
    """
    text = _MD_BOLD_RE.sub(r"\1", text)           # **texto** → texto
    text = _MD_UNDERLINE_RE.sub(r"\1", text)       # __texto__ → texto
    text = _STAGE_DIRECTION_RE.sub("", text)       # *stage* → remove
    text = _TIMING_ANNOTATION_RE.sub("", text)     # (0-5s) → remove
    text = _BRACKET_LABEL_RE.sub("", text)         # [10-40 segundos] → remove
    text = _WORD_COUNT_NOTE_RE.sub("", text)       # (100 palavras) → remove
    text = re.sub(r"[*#_]+", "", text)             # asteriscos/hashes soltos
    return text.strip()


def _extract_narration(text: str) -> str:
    """
    Extrai o texto puro de narração do roteiro completo para uso no TTS (módulo A2).

    Por que programático (não 5ª chamada LLM):
      - Determinístico: mesma entrada → mesma saída, sem retry.
      - Rápido: não consome GPU nem tempo de geração extra.
      - Testável: resultado exato verificável em testes unitários.

    Remove (nesta ordem):
      1. Blocos <think>…</think> do Qwen3 (segurança adicional ao _call_llm).
      2. Linhas de separação horizontal (---, ***, ___).
      3. Linhas que são apenas rótulos de seção Markdown:
           - Cabeçalhos (#, ##, …)
           - Linhas bold-only com timing — **Gancho (0-5s):**
           - Direções de cena solitárias — *clique de relógio*
      4. Direções de cena inline em linhas com narração real.
      5. Formatação Markdown (**negrito**, __sublinhado__) — mantém o texto.
      6. Anotações de tempo — (0-5s), [00:30].
      7. Emojis.
      8. O que a voz não deve ler como está escrito: "Ex:" vira "Por exemplo,",
         aspas sem par na mesma linha somem e marcadores de lista viram fala
         ("1." → "Primeiro,") ou somem ("-", "•", números acima de 10).
      9. Whitespace extra e linhas em branco múltiplas.

    Args:
        text: full_script gerado pelo LLM.

    Returns:
        Texto limpo, pronto para síntese de voz.
    """
    # 1. Remove think blocks
    text = _strip_think_blocks(text)

    # 2. Remove linhas de separação horizontal
    text = _HR_RE.sub("", text)

    cleaned: list[str] = []

    for raw_line in text.split("\n"):
        line = raw_line.strip()

        # Pula linhas vazias (serão normalizadas no final)
        if not line:
            cleaned.append("")
            continue

        # 3a. Linha é cabeçalho Markdown (#, ##, …) → remove
        if _MD_HEADER_LINE_RE.match(line):
            continue

        # 3b. Determina se a linha é apenas um rótulo de seção:
        #     Após remover toda formatação, verifica se é vazio (stage direction solo)
        #     ou se termina com ":" E contém timing do tipo (0-5s) → é um rótulo.
        no_md = _normalize_markdown(line)
        is_label = (
            not no_md  # linha era só formatação/stage direction
            or (no_md.endswith(":") and _SECTION_TIMING_RE.search(line))
        )
        if is_label:
            continue

        # 4. Strip asteriscos de ênfase/cena inline — preserva o texto interno.
        # _normalize_markdown (passo 3b) já descartou linhas que SÓ tinham *stage*,
        # então o que chega aqui é ênfase em texto real: *limpeza* → limpeza.
        line = _STAGE_DIRECTION_RE.sub(r"\1", line)

        # 5. Strip Markdown (mantém texto do conteúdo)
        line = _MD_BOLD_RE.sub(r"\1", line)
        line = _MD_UNDERLINE_RE.sub(r"\1", line)

        # 6. Remove anotações de tempo, rótulos entre colchetes e contagem de palavras
        line = _TIMING_ANNOTATION_RE.sub("", line)
        line = _BRACKET_LABEL_RE.sub("", line)
        line = _WORD_COUNT_NOTE_RE.sub("", line)
        line = _HASHTAG_RE.sub("", line)

        # 7. Remove emojis
        line = _EMOJI_RE.sub("", line)

        # Remove asteriscos/hashes soltos que sobraram
        line = re.sub(r"[*#_]+", "", line)

        # 8. Escreve como a voz deve falar: "Ex:" por extenso, sem aspas sem par
        #    e sem marcador de lista com hífen ou bolinha.
        line = _EXAMPLE_ABBREVIATION_RE.sub(
            lambda match: "Por exemplo," if match.group(1) == "E" else "por exemplo,",
            line,
        )
        line = _drop_unpaired_quotes(line)

        # Normaliza espaços internos
        line = re.sub(r"  +", " ", line).strip()
        line = _BULLET_MARKER_RE.sub("", line)

        if line:
            cleaned.append(line)

    # Reconstrói, colapsando sequências de linhas em branco para no máximo uma
    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return _speak_list_markers(result.strip())


def _drop_unpaired_quotes(line: str) -> str:
    """Remove as aspas que não abrem e fecham na mesma linha."""
    open_positions: dict[str, list[int]] = {}
    unpaired: set[int] = set()
    for index, char in enumerate(line):
        before = line[index - 1] if index else ""
        after = line[index + 1] if index + 1 < len(line) else ""
        if char in "'’" and before.isalnum() and after.isalnum():
            continue  # apóstrofo: d'água
        if char in _SYMMETRIC_QUOTES:
            kind = char
            opening = not before or before.isspace() or before in "([{—–"
        elif char in _OPENING_QUOTES:
            kind, opening = char, True
        elif char in _CLOSING_QUOTES:
            kind, opening = _CLOSING_QUOTES[char], False
        else:
            continue
        stack = open_positions.setdefault(kind, [])
        if opening:
            stack.append(index)
        elif stack:
            stack.pop()
        else:
            unpaired.add(index)
    for stack in open_positions.values():
        unpaired.update(stack)
    return "".join(char for index, char in enumerate(line) if index not in unpaired)


def _speak_list_markers(text: str) -> str:
    """Troca "1." no início da linha por "Primeiro," (até 10) ou remove o número.

    A inicial da palavra seguinte só vira minúscula quando o próprio texto já usa
    essa palavra em minúscula ("O que" → "o que"); um nome próprio fica como está.
    """
    lines: list[str] = []
    for line in text.split("\n"):
        marker = _NUMBERED_MARKER_RE.match(line)
        if marker is None:
            lines.append(line)
            continue
        rest = line[marker.end():]
        number = int(marker.group(1))
        if not 1 <= number <= len(_SPOKEN_ORDINALS):
            lines.append(rest)
            continue
        first = _FIRST_WORD_RE.match(rest)
        if first is not None:
            word = first.group(1)
            lowered = word.lower()
            if word[0].isupper() and word[1:] == lowered[1:] and re.search(
                rf"(?<!\w){re.escape(lowered)}(?!\w)", text
            ):
                rest = lowered + rest[len(word):]
        lines.append(f"{_SPOKEN_ORDINALS[number - 1]}, {rest}")
    return "\n".join(lines)


# ── Cliente ───────────────────────────────────────────────────────────────────


def _make_client() -> OpenAI:
    """Cria um cliente OpenAI apontando para o Ollama local via settings."""
    return OpenAI(
        base_url=settings.ollama_base_url,
        api_key="ollama",
        timeout=float(settings.ollama_timeout),
    )


# ── Chamada LLM com retry ─────────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((APITimeoutError, APIConnectionError, EmptyResponseError)),
    before_sleep=before_sleep_log(_std_log, logging.WARNING),
    reraise=True,
)
def _call_llm(
    client: OpenAI,
    *,
    system: str,
    user: str,
    step: str,
) -> str:
    """
    Faz uma única chamada ao LLM com retry automático (3 tentativas, backoff exponencial).

    Comportamentos especiais:
      - Blocos <think>…</think> são removidos ANTES de qualquer validação.
        Se a resposta inteira for só um bloco think (sem conteúdo real),
        EmptyResponseError é lançado e a chamada é repetida.

    Raises:
        APITimeoutError: após 3 tentativas com timeout.
        APIConnectionError: após 3 tentativas com falha de conexão.
        EmptyResponseError: após 3 tentativas com resposta vazia ou só think.
    """
    log.info("llm_step_start", step=step, model=settings.ollama_model)
    try:
        response = client.chat.completions.create(
            model=settings.ollama_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # CAMADA 1 — Ollama aceita {"think": false} para desabilitar o thinking
            # mode do Qwen3 diretamente no servidor, antes de qualquer token ser gerado.
            # Outros modelos no Ollama ignoram parâmetros desconhecidos sem erro.
            extra_body={"think": False},
        )
    except (APITimeoutError, APIConnectionError):
        log.warning("llm_step_retry", step=step, reason="connection_or_timeout")
        raise

    raw: str | None = response.choices[0].message.content

    # CAMADA 2 — sanitize_llm_output remove qualquer artefato de raciocínio que
    # tenha escapado da Camada 1 (blocos completos, </think> órfão, "nowrap", etc.).
    # Se após a limpeza o conteúdo ficar vazio, é sinal de que a resposta era só
    # raciocínio sem output real → EmptyResponseError dispara o retry.
    content = sanitize_llm_output(raw or "")

    if not content:
        log.warning("llm_step_retry", step=step, reason="empty_after_think_strip")
        raise EmptyResponseError(
            f"Ollama retornou resposta vazia (ou só <think>) na etapa '{step}'"
        )

    log.info("llm_step_done", step=step, chars=len(content))
    return content


# ── Contexto por formato ──────────────────────────────────────────────────────


_FORMAT_CONTEXT: dict[str, str] = {
    "short": (
        "YouTube Short (vídeo vertical de até 60 segundos, ~100–150 palavras no roteiro). "
        "O conteúdo deve ser direto, impactante e funcionar sem contexto externo."
    ),
    "long": (
        "vídeo longo do YouTube (10+ minutos, roteiro de 1.500–2.500 palavras). "
        "Pode ter introdução, desenvolvimento em seções e conclusão com profundidade."
    ),
}


def _system_base(persona: str, fmt: Literal["short", "long"]) -> str:
    """
    Base de todos os system prompts — inclui:
      - /no_think: instrução Qwen3 para desabilitar thinking mode (Camada 1).
      - Persona, formato e regra de idioma PT-BR.
    """
    return (
        # Token Qwen3 que desativa o modo thinking. Outros modelos ignoram essa linha.
        "/no_think\n"
        "Você é um roteirista profissional de YouTube para o mercado brasileiro (PT-BR).\n"
        f"Persona do canal: {persona}\n"
        f"Formato do vídeo: {_FORMAT_CONTEXT[fmt]}\n"
        "Escreva sempre em português do Brasil, de forma natural e envolvente."
        f"{_LANGUAGE_RULE}"
    )


def _facts_rule(facts: Sequence[SourcedFact]) -> str:
    """Bloco anexado a todos os system prompts: só entra dado que o operador forneceu."""
    if not facts:
        return (
            "\n\nFATOS – REGRA ABSOLUTA:\n"
            "Você não recebeu fontes para esta pauta. NÃO cite números, percentuais, "
            "proporções (como 'o dobro' ou 'três vezes mais'), estatísticas, estudos, "
            "pesquisas, instituições ou especialistas. Explique o tema de forma "
            "qualitativa, com exemplos do cotidiano."
        )
    listed = "\n".join(f"  • {fact.claim} (fonte: {fact.source})" for fact in facts)
    return (
        "\n\nFATOS – REGRA ABSOLUTA:\n"
        "Os únicos números, estatísticas e estudos que você pode citar são os da lista "
        "abaixo, sem alterar os valores. NÃO invente nenhum outro dado.\n"
        f"{listed}"
    )


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_NUMBER_WORDS: dict[str, str] = {
    "um": "1", "uma": "1", "dois": "2", "duas": "2", "três": "3", "quatro": "4",
    "cinco": "5", "seis": "6", "sete": "7", "oito": "8", "nove": "9", "dez": "10",
}
# Proporções por extenso: as duas afirmações inventadas na run 11 tinham esta forma.
_PROPORTION_RE = re.compile(
    r"\b(?:dobr[ao]m?|dobrar|triplo|triplicam?|metade|por cento|milh(?:ão|ões)|bilh(?:ão|ões)"
    r"|(?:duas|três|quatro|cinco|seis|sete|oito|nove|dez|cem)\s+vezes\s+"
    r"(?:mais|menos|maior|menor|melhor|pior))\b",
    re.IGNORECASE,
)
_AUTHORITY_RE = re.compile(
    r"\b(?:estudos?|pesquisas?|cientistas|pesquisadores|especialistas|a ciência|dados)\s+"
    r"(?:já\s+)?(?:mostr|revel|indic|comprov|apont|diz|afirm|suger|confirm|prov|descobr)\w*"
    r"|\b(?:segundo|de acordo com)\s+(?:\w+\s+){0,2}(?:estudos?|pesquisas?|cientistas"
    r"|pesquisadores|especialistas|a ciência)\b",
    re.IGNORECASE,
)
_MAX_UNSOURCED_CLAIMS = 20


def find_unsourced_claims(
    narration: str,
    *,
    topic: str,
    facts: Sequence[SourcedFact],
    also_supported: str = "",
) -> list[str]:
    """
    Lista as frases da narração com dado que nem a pauta nem os fatos sustentam.

    É uma heurística determinística para orientar a revisão humana, não uma
    checagem de fatos: marca números ausentes da pauta e dos fatos, proporções
    por extenso e, quando nenhum fato foi fornecido, apelos a estudos.
    also_supported traz números que o próprio pedido introduz, como a quantidade
    de itens de uma lista.
    """
    supported = " ".join(
        [topic, also_supported, *(fact.claim for fact in facts)]
    ).casefold()
    allowed_numbers = set(_NUMBER_RE.findall(supported))
    allowed_numbers.update(
        digit for word, digit in _NUMBER_WORDS.items() if re.search(rf"\b{word}\b", supported)
    )
    claims: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(narration):
        sentence = raw.strip()
        if not sentence or sentence in claims:
            continue
        unsupported_number = any(
            number not in allowed_numbers for number in _NUMBER_RE.findall(sentence)
        )
        unsupported_proportion = any(
            match.group(0).casefold() not in supported
            for match in _PROPORTION_RE.finditer(sentence)
        )
        appeals_to_authority = not facts and _AUTHORITY_RE.search(sentence) is not None
        if unsupported_number or unsupported_proportion or appeals_to_authority:
            claims.append(sentence)
        if len(claims) == _MAX_UNSOURCED_CLAIMS:
            break
    return claims


# ── Prompts das 4 etapas ──────────────────────────────────────────────────────


def _prompts_angle(
    topic: str,
    persona: str,
    fmt: Literal["short", "long"],
) -> tuple[str, str]:
    """Etapa 1: ângulo editorial específico e diferenciado."""
    system = (
        f"{_system_base(persona, fmt)}\n\n"
        "Sua tarefa: definir o ÂNGULO EDITORIAL do vídeo — um ponto de vista específico, "
        "surpreendente ou contra-intuitivo sobre o tema. Evite ângulos óbvios e genéricos.\n"
        "Responda APENAS com o ângulo em 1–3 frases diretas, sem introdução."
    )
    user = (
        f"Tema da pauta: {topic}\n\n"
        "Defina o ângulo editorial específico para este vídeo."
    )
    return system, user


def _prompts_outline(
    topic: str,
    angle: str,
    persona: str,
    fmt: Literal["short", "long"],
    structure: Structure = "direct",
) -> tuple[str, str]:
    """Etapa 2: outline estruturado com base no ângulo."""
    structure_hint = _STRUCTURE_OUTLINE[structure][fmt]

    system = (
        f"{_system_base(persona, fmt)}\n\n"
        "Sua tarefa: criar o OUTLINE (estrutura) do roteiro a partir do ângulo definido.\n"
        f"{structure_hint}"
    )
    user = (
        f"Tema: {topic}\n"
        f"Ângulo editorial: {angle}\n\n"
        "Crie o outline do roteiro."
    )
    return system, user


_HOOK_LENGTH: dict[str, str] = {
    "short": "uma ou duas frases curtas, com no máximo 25 palavras",
    "long": "os primeiros 15–30 segundos de fala, entre 40 e 80 palavras",
}


def _prompts_hook(
    topic: str,
    angle: str,
    outline: str,
    persona: str,
    fmt: Literal["short", "long"],
    recent_openings: list[str] | None,
    *,
    opening_style: OpeningStyle | None = None,
    refused: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """
    Etapa 3: hook — a abertura que será narrada antes do restante do roteiro.

    Se recent_openings for fornecido, o system prompt instrui o modelo a
    NÃO repetir nenhuma dessas aberturas (variação anti-template).
    opening_style pede um tipo de abertura; refused traz o hook recusado na
    tentativa anterior e o motivo.
    """
    style_block = ""
    if opening_style is not None:
        style_block = f"\n\nESTILO DA ABERTURA – OBRIGATÓRIO:\n{_OPENING_STYLE_RULES[opening_style]}"
        if refused is not None:
            # Só "não seguiu o estilo" não bastou: o modelo repetiu a pergunta três vezes.
            style_block += (
                f" A tentativa anterior foi recusada porque {refused[1]}: «{refused[0]}». "
                "Escreva uma abertura diferente dessa."
            )

    openings_block = ""
    if recent_openings:
        listed = "\n".join(f"  • {o}" for o in recent_openings)
        openings_block = (
            "\n\nATENÇÃO – VARIAÇÃO OBRIGATÓRIA:\n"
            "Os vídeos recentes do canal usaram estas aberturas (NÃO repita nenhuma delas):\n"
            f"{listed}\n"
            "Use uma abertura completamente diferente em estrutura, palavras e tom."
        )

    system = (
        f"{_system_base(persona, fmt)}\n\n"
        "Sua tarefa: escrever o HOOK — a abertura falada do vídeo, que prende a atenção "
        "do espectador imediatamente, cria curiosidade e deixa claro por que vale "
        f"assistir até o fim. Tamanho: {_HOOK_LENGTH[fmt]}.\n"
        "Este texto será narrado exatamente como você escrever: responda APENAS com o "
        "texto falado, sem emojis, rótulos, aspas em volta ou comentários extras."
        f"{style_block}"
        f"{openings_block}"
    )
    user = (
        f"Tema: {topic}\n"
        f"Ângulo editorial: {angle}\n"
        f"Outline:\n{outline}\n\n"
        "Escreva o hook."
    )
    return system, user


def _prompts_full_script(
    topic: str,
    angle: str,
    outline: str,
    hook: str,
    persona: str,
    fmt: Literal["short", "long"],
    *,
    structure: Structure = "direct",
    body_words: tuple[int, int] | None = None,
    previous_words: int | None = None,
) -> tuple[str, str]:
    """
    Etapa 4: restante do roteiro, que continua a partir do hook já escrito.

    body_words é a faixa de palavras que cabe ao restante depois de descontado o
    hook; previous_words informa o tamanho de uma tentativa recusada.
    """
    length_block = ""
    if body_words is not None:
        length_block = (
            f"\n\nTAMANHO OBRIGATÓRIO: escreva entre {body_words[0]} e {body_words[1]} "
            "palavras faladas, sem contar a abertura. Não anote a contagem na resposta."
        )
        if previous_words is not None:
            # Só o número recusado não bastou: o modelo devolveu o mesmo tamanho três vezes.
            if previous_words < body_words[0]:
                correction = (
                    f"curta demais: escreva pelo menos {body_words[0] - previous_words} "
                    "palavras a mais"
                )
            else:
                correction = (
                    f"longa demais: corte pelo menos {previous_words - body_words[1]} palavras"
                )
            length_block += (
                f" A tentativa anterior ficou com {previous_words} palavras e foi recusada "
                f"por estar {correction}."
            )

    system = (
        f"{_system_base(persona, fmt)}\n\n"
        "Sua tarefa: escrever o RESTANTE DO ROTEIRO, seção por seção, seguindo o outline. "
        "A abertura do vídeo já está escrita e será narrada antes do seu texto: "
        "continue a partir dela, sem repeti-la nem reescrevê-la. "
        "Inclua falas naturais, transições e linguagem coloquial PT-BR. "
        "Escreva APENAS o texto falado — sem didascálias de câmera ou stage directions, "
        "sem rótulos de seção, marcações de tempo ou aspas em volta do texto."
        f"{_STRUCTURE_BODY[structure]}"
        f"{length_block}"
    )
    user = (
        f"Tema: {topic}\n"
        f"Ângulo editorial: {angle}\n"
        f"Outline:\n{outline}\n"
        f"Abertura já escrita:\n{hook}\n\n"
        "Escreva o restante do roteiro."
    )
    return system, user


def _drop_repeated_opening(body: str, hook: str) -> str:
    """Remove o hook quando o modelo o repete no início do restante do roteiro."""
    words = hook.split()
    if not words:
        return body
    repeated = re.match(
        r"[\s\"“”*_]*" + r"\s+".join(re.escape(word) for word in words) + r"[\s\"“”*_]*",
        body,
        re.IGNORECASE,
    )
    return body[repeated.end():] if repeated else body


def _count_words(text: str) -> int:
    """Conta as palavras faladas: tokens com ao menos uma letra ou dígito."""
    return sum(1 for token in text.split() if any(char.isalnum() for char in token))


# ── Função principal ──────────────────────────────────────────────────────────


def generate_script(
    topic: str,
    persona: str,
    fmt: Literal["short", "long"],
    recent_openings: list[str] | None = None,
    *,
    facts: Sequence[SourcedFact] = (),
    opening_style: OpeningStyle | None = None,
    structure: Structure | None = None,
    client: OpenAI | None = None,
) -> ScriptResult:
    """
    Gera um roteiro estruturado em 4 etapas encadeadas via Ollama.

    Args:
        topic: Tema/pauta do vídeo.
        persona: Identidade editorial do canal (tom, estilo, público-alvo).
        fmt: Formato — 'short' (até 60s) ou 'long' (10+ min).
        recent_openings: Aberturas usadas em vídeos recentes (anti-template).
        facts: Fatos com fonte fornecidos na pauta; são os únicos dados citáveis.
        opening_style: Tipo de abertura pedido para o hook; None não pede nenhum.
        structure: Estrutura do roteiro; None usa a estrutura direta.
        client: Cliente OpenAI injetável para testes. Se None, cria do settings.

    Returns:
        ScriptResult com angle, outline, full_script, hook e narration.
        O campo `narration` é o texto limpo para o TTS (sem markdown/emojis/think).
        `unsourced_claims` lista as frases que a revisão humana precisa conferir.
    """
    if client is None:
        client = _make_client()

    log.info("script_gen_start", topic=topic, fmt=fmt, facts=len(facts))
    facts_rule = _facts_rule(facts)

    sys1, usr1 = _prompts_angle(topic, persona, fmt)
    angle = _call_llm(client, system=sys1 + facts_rule, user=usr1, step="angle")

    script_structure: Structure = structure or "direct"
    sys2, usr2 = _prompts_outline(topic, angle, persona, fmt, script_structure)
    outline = _call_llm(client, system=sys2 + facts_rule, user=usr2, step="outline")

    # Um hook fora do estilo pedido é refeito; se continuar fora, segue marcado para
    # a revisão humana em vez de derrubar a run.
    opening_style_met: bool | None = None
    refused: tuple[str, str] | None = None
    for attempt in range(1, _OPENING_ATTEMPTS + 1):
        sys3, usr3 = _prompts_hook(
            topic,
            angle,
            outline,
            persona,
            fmt,
            recent_openings,
            opening_style=opening_style,
            refused=refused,
        )
        # O hook passa pela mesma limpeza da narração: o texto revisado é o texto falado.
        # Aspas em volta do hook inteiro são do modelo, não da fala.
        hook = _extract_narration(
            _call_llm(client, system=sys3 + facts_rule, user=usr3, step="hook")
        ).strip("\"“”")
        if opening_style is None:
            break
        problem = _opening_style_problem(hook, opening_style)
        opening_style_met = problem is None
        if problem is None:
            break
        log.warning("script_opening_style_missed", style=opening_style, attempt=attempt)
        refused = (hook, problem)

    # O prompt sozinho não segura o tamanho: a narração é contada e, fora da faixa,
    # só o restante do roteiro é refeito, com o tamanho recusado informado ao modelo.
    word_range = _NARRATION_WORD_RANGE.get(fmt)
    hook_words = _count_words(hook)
    body_range = (
        None
        if word_range is None
        else (max(1, word_range[0] - hook_words), max(1, word_range[1] - hook_words))
    )
    previous_words: int | None = None
    for attempt in range(1, _LENGTH_ATTEMPTS + 1):
        sys4, usr4 = _prompts_full_script(
            topic,
            angle,
            outline,
            hook,
            persona,
            fmt,
            structure=script_structure,
            body_words=body_range,
            previous_words=previous_words,
        )
        body = _drop_repeated_opening(
            _call_llm(client, system=sys4 + facts_rule, user=usr4, step="full_script"),
            hook,
        )
        full_script = f"{hook}\n\n{body}"

        # Limpeza programática: mais rápida, determinística e sem custo de GPU extra
        # vs. uma 5ª chamada LLM (que adicionaria ~30s e poderia introduzir novos artefatos).
        narration = _extract_narration(full_script)
        words = _count_words(narration)
        if word_range is None or word_range[0] <= words <= word_range[1]:
            break
        log.warning(
            "script_length_out_of_range",
            fmt=fmt,
            attempt=attempt,
            words=words,
            minimum=word_range[0],
            maximum=word_range[1],
        )
        previous_words = max(0, words - hook_words)
    else:
        raise ScriptLengthError(
            f"narração com {words} palavras após {_LENGTH_ATTEMPTS} tentativas "
            f"no formato '{fmt}'"
        )

    unsourced_claims = find_unsourced_claims(
        narration,
        topic=topic,
        facts=facts,
        also_supported=_STRUCTURE_SUPPORTED_NUMBERS.get(script_structure, {}).get(fmt, ""),
    )

    log.info(
        "script_gen_done",
        topic=topic,
        fmt=fmt,
        narration_chars=len(narration),
        narration_words=words,
        unsourced_claims=len(unsourced_claims),
    )

    return ScriptResult(
        angle=angle,
        outline=outline,
        full_script=full_script,
        hook=hook,
        narration=narration,
        unsourced_claims=unsourced_claims,
        opening_style=opening_style,
        opening_style_met=opening_style_met,
        structure=structure,
    )


# ── Persistência ──────────────────────────────────────────────────────────────


def save_script(result: ScriptResult, channel_id: int, video_id: int) -> str:
    """
    Salva full_script e narration em storage/{channel_id}/{video_id}/.

    Arquivos gerados:
      script.txt    — roteiro completo com formatação (para revisão humana)
      narration.txt — texto puro para TTS (consumido pelo módulo A2)

    Returns:
        Caminho de script.txt (string).
    """
    base_dir = Path("storage") / str(channel_id) / str(video_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    script_path = base_dir / "script.txt"
    script_path.write_text(result.full_script, encoding="utf-8")

    narration_path = base_dir / "narration.txt"
    narration_path.write_text(result.narration, encoding="utf-8")

    log.info(
        "script_saved",
        path=str(script_path),
        channel_id=channel_id,
        video_id=video_id,
    )
    return str(script_path)
