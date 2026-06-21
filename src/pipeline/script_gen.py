"""
A1 — Geração de roteiro (ARQUITETURA.md Seção A1).

Pipeline de 4 etapas encadeadas via Ollama (cliente OpenAI-compatível):
  1. Ângulo editorial específico
  2. Outline estruturado
  3. Roteiro completo seção por seção
  4. Hook dos primeiros 15–30 segundos

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
from pathlib import Path
from typing import Literal

import structlog
from openai import APIConnectionError, APITimeoutError, OpenAI
from pydantic import BaseModel
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
    hook: str         # Abertura dos primeiros 15–30 segundos
    narration: str    # Texto puro para TTS: sem emojis, markdown, stage directions


class EmptyResponseError(RuntimeError):
    """Lançada quando o Ollama retorna resposta vazia ou só whitespace."""


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
    "\U0001F300-\U0001F9FF"   # Emoticons, misc symbols, transport, flags, etc.
    "\U0001FA00-\U0001FAFF"   # Símbolos e pictogramas recentes
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
      8. Whitespace extra e linhas em branco múltiplas.

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

        # 6. Remove anotações de tempo
        line = _TIMING_ANNOTATION_RE.sub("", line)

        # 7. Remove emojis
        line = _EMOJI_RE.sub("", line)

        # Remove asteriscos/hashes soltos que sobraram
        line = re.sub(r"[*#_]+", "", line)

        # Normaliza espaços internos
        line = re.sub(r"  +", " ", line).strip()

        if line:
            cleaned.append(line)

    # Reconstrói, colapsando sequências de linhas em branco para no máximo uma
    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


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
) -> tuple[str, str]:
    """Etapa 2: outline estruturado com base no ângulo."""
    if fmt == "long":
        structure_hint = "Crie 3–5 seções com títulos e 2–3 pontos-chave por seção."
    else:
        structure_hint = "Crie 3 blocos curtos: gancho, desenvolvimento rápido e call-to-action."

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


def _prompts_full_script(
    topic: str,
    angle: str,
    outline: str,
    persona: str,
    fmt: Literal["short", "long"],
    recent_openings: list[str] | None,
) -> tuple[str, str]:
    """
    Etapa 3: roteiro completo, seção por seção.

    Se recent_openings for fornecido, o system prompt instrui o modelo a
    NÃO repetir nenhuma dessas aberturas (variação anti-template).
    """
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
        "Sua tarefa: escrever o ROTEIRO COMPLETO, seção por seção, seguindo o outline. "
        "Inclua falas naturais, transições e linguagem coloquial PT-BR. "
        "Escreva APENAS o texto falado — sem didascálias de câmera ou stage directions."
        f"{openings_block}"
    )
    user = (
        f"Tema: {topic}\n"
        f"Ângulo editorial: {angle}\n"
        f"Outline:\n{outline}\n\n"
        "Escreva o roteiro completo."
    )
    return system, user


def _prompts_hook(
    full_script: str,
    persona: str,
    fmt: Literal["short", "long"],
) -> tuple[str, str]:
    """Etapa 4: hook — os primeiros 15–30 segundos do vídeo."""
    system = (
        f"{_system_base(persona, fmt)}\n\n"
        "Sua tarefa: extrair/refinar o HOOK do roteiro — os primeiros 15–30 segundos "
        "de fala que prendem a atenção do espectador imediatamente. "
        "O hook deve criar curiosidade e deixar claro por que vale assistir o vídeo inteiro.\n"
        "Responda APENAS com o texto do hook, sem comentários extras."
    )
    user = (
        f"Roteiro completo:\n{full_script}\n\n"
        "Extraia ou refine o hook (abertura de 15–30 segundos)."
    )
    return system, user


# ── Função principal ──────────────────────────────────────────────────────────


def generate_script(
    topic: str,
    persona: str,
    fmt: Literal["short", "long"],
    recent_openings: list[str] | None = None,
    *,
    client: OpenAI | None = None,
) -> ScriptResult:
    """
    Gera um roteiro estruturado em 4 etapas encadeadas via Ollama.

    Args:
        topic: Tema/pauta do vídeo.
        persona: Identidade editorial do canal (tom, estilo, público-alvo).
        fmt: Formato — 'short' (até 60s) ou 'long' (10+ min).
        recent_openings: Aberturas usadas em vídeos recentes (anti-template).
        client: Cliente OpenAI injetável para testes. Se None, cria do settings.

    Returns:
        ScriptResult com angle, outline, full_script, hook e narration.
        O campo `narration` é o texto limpo para o TTS (sem markdown/emojis/think).
    """
    if client is None:
        client = _make_client()

    log.info("script_gen_start", topic=topic, fmt=fmt)

    sys1, usr1 = _prompts_angle(topic, persona, fmt)
    angle = _call_llm(client, system=sys1, user=usr1, step="angle")

    sys2, usr2 = _prompts_outline(topic, angle, persona, fmt)
    outline = _call_llm(client, system=sys2, user=usr2, step="outline")

    sys3, usr3 = _prompts_full_script(topic, angle, outline, persona, fmt, recent_openings)
    full_script = _call_llm(client, system=sys3, user=usr3, step="full_script")

    sys4, usr4 = _prompts_hook(full_script, persona, fmt)
    hook = _call_llm(client, system=sys4, user=usr4, step="hook")

    # Limpeza programática: mais rápida, determinística e sem custo de GPU extra
    # vs. uma 5ª chamada LLM (que adicionaria ~30s e poderia introduzir novos artefatos).
    narration = _extract_narration(full_script)

    log.info("script_gen_done", topic=topic, fmt=fmt, narration_chars=len(narration))

    return ScriptResult(
        angle=angle,
        outline=outline,
        full_script=full_script,
        hook=hook,
        narration=narration,
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
