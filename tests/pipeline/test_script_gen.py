"""
Testes de src/pipeline/script_gen.py — cliente Ollama totalmente mockado.

Cobertura original (17 testes):
- As 4 etapas LLM são chamadas na ordem correta.
- recent_openings aparece no system prompt da etapa full_script.
- Sem recent_openings, o bloco de restrição NÃO aparece.
- ScriptResult é construído com os campos corretos.
- Formato 'short' usa contexto de duração correto nos prompts.
- _call_llm retenta em APITimeoutError (com sucesso na 3ª tentativa).
- _call_llm retenta em resposta vazia (EmptyResponseError).
- Após 3 falhas, a exceção é relançada.
- save_script cria o arquivo no caminho correto e retorna string.

Novos testes (v2):
- _call_llm remove blocos <think> antes de retornar.
- _call_llm retenta quando a resposta é APENAS um bloco <think> (sem conteúdo real).
- _extract_narration remove emojis, markdown, stage directions, rótulos de seção.
- Campo `narration` do ScriptResult não contém emojis nem markdown.
- save_script grava também narration.txt.
- Regra de idioma PT-BR está presente em todos os system prompts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import APITimeoutError

from src.pipeline.script_gen import (
    ScriptResult,
    _call_llm,
    _extract_narration,
    _strip_think_blocks,  # alias para sanitize_llm_output (compatibilidade)
    generate_script,
    sanitize_llm_output,
    save_script,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _fake_response(content: str) -> MagicMock:
    """Cria um mock de ChatCompletion com o conteúdo fornecido."""
    resp = MagicMock()
    resp.choices[0].message.content = content
    return resp


def _client_with(*responses: str) -> MagicMock:
    """
    Mock do cliente OpenAI que retorna as respostas fornecidas em sequência.
    Cada chamada a chat.completions.create consome um item da fila.
    """
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _fake_response(r) for r in responses
    ]
    return client


def _timeout_error() -> APITimeoutError:
    """Cria um APITimeoutError com request httpx mínimo."""
    req = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")
    return APITimeoutError(request=req)


# ── Testes de generate_script ─────────────────────────────────────────────────


def test_exactly_four_llm_calls() -> None:
    """Verifica que generate_script faz exatamente 4 chamadas ao LLM."""
    client = _client_with("ângulo X", "outline Y", "roteiro Z", "hook W")

    generate_script(
        topic="IA no trabalho",
        persona="Especialista em tecnologia",
        fmt="long",
        client=client,
    )

    assert client.chat.completions.create.call_count == 4


def test_script_result_fields_match_llm_responses() -> None:
    """ScriptResult deve ter cada campo preenchido com a resposta da etapa correspondente."""
    client = _client_with("Ângulo A", "Outline B", "Roteiro C", "Hook D")

    result = generate_script(
        topic="Python em 2026",
        persona="Dev sênior descontraído",
        fmt="short",
        client=client,
    )

    assert isinstance(result, ScriptResult)
    assert result.angle == "Ângulo A"
    assert result.outline == "Outline B"
    assert result.full_script == "Roteiro C"
    assert result.hook == "Hook D"


def test_recent_openings_appear_in_full_script_system_prompt() -> None:
    """
    Etapa 3 (full_script) deve incluir as recent_openings no system prompt,
    com instrução explícita para não repeti-las.
    """
    client = _client_with("ângulo", "outline", "roteiro", "hook")
    openings = ["Você sabia que...", "Hoje eu vou te mostrar..."]

    generate_script(
        topic="Produtividade com IA",
        persona="Coach de produtividade",
        fmt="long",
        recent_openings=openings,
        client=client,
    )

    # Terceira chamada (índice 2) é a etapa full_script
    third_call = client.chat.completions.create.call_args_list[2]
    messages: list[dict[str, str]] = third_call.kwargs["messages"]
    system_prompt = next(m["content"] for m in messages if m["role"] == "system")

    assert "Você sabia que..." in system_prompt
    assert "Hoje eu vou te mostrar..." in system_prompt
    assert "VARIAÇÃO OBRIGATÓRIA" in system_prompt


def test_no_recent_openings_omits_restriction_block() -> None:
    """Sem recent_openings, o bloco de restrição NÃO deve aparecer no prompt."""
    client = _client_with("ângulo", "outline", "roteiro", "hook")

    generate_script(
        topic="Machine Learning",
        persona="Cientista de dados",
        fmt="long",
        recent_openings=None,
        client=client,
    )

    third_call = client.chat.completions.create.call_args_list[2]
    messages: list[dict[str, str]] = third_call.kwargs["messages"]
    system_prompt = next(m["content"] for m in messages if m["role"] == "system")

    assert "VARIAÇÃO OBRIGATÓRIA" not in system_prompt


def test_short_format_mentions_duration_in_prompt() -> None:
    """Formato 'short' deve incluir referência à duração (60 segundos / Short) nos prompts."""
    client = _client_with("ângulo", "outline", "roteiro", "hook")

    generate_script(
        topic="Dica de Python",
        persona="Dev pythonista",
        fmt="short",
        client=client,
    )

    # Primeira chamada — ângulo — já usa o contexto de formato
    first_call = client.chat.completions.create.call_args_list[0]
    messages: list[dict[str, str]] = first_call.kwargs["messages"]
    system_prompt = next(m["content"] for m in messages if m["role"] == "system")

    assert "Short" in system_prompt or "60 segundo" in system_prompt


def test_long_format_mentions_duration_in_prompt() -> None:
    """Formato 'long' deve incluir referência a minutos no system prompt."""
    client = _client_with("ângulo", "outline", "roteiro", "hook")

    generate_script(
        topic="Arquitetura de software",
        persona="Arquiteto de sistemas",
        fmt="long",
        client=client,
    )

    first_call = client.chat.completions.create.call_args_list[0]
    messages: list[dict[str, str]] = first_call.kwargs["messages"]
    system_prompt = next(m["content"] for m in messages if m["role"] == "system")

    assert "minuto" in system_prompt or "longo" in system_prompt


def test_outline_receives_angle_from_step1() -> None:
    """
    A etapa 2 (outline) deve receber o ângulo retornado pela etapa 1
    no seu user prompt — encadeamento das etapas.
    """
    client = _client_with("ÂNGULO_ESPECIFICO", "outline", "roteiro", "hook")

    generate_script(
        topic="Finanças pessoais",
        persona="Educador financeiro",
        fmt="long",
        client=client,
    )

    second_call = client.chat.completions.create.call_args_list[1]
    messages: list[dict[str, str]] = second_call.kwargs["messages"]
    user_prompt = next(m["content"] for m in messages if m["role"] == "user")

    assert "ÂNGULO_ESPECIFICO" in user_prompt


def test_hook_receives_full_script_from_step3() -> None:
    """
    A etapa 4 (hook) deve receber o roteiro completo retornado pela etapa 3
    no seu user prompt — encadeamento das etapas.
    """
    client = _client_with("ângulo", "outline", "ROTEIRO_COMPLETO_XYZ", "hook")

    generate_script(
        topic="Saúde mental",
        persona="Psicóloga acessível",
        fmt="long",
        client=client,
    )

    fourth_call = client.chat.completions.create.call_args_list[3]
    messages: list[dict[str, str]] = fourth_call.kwargs["messages"]
    user_prompt = next(m["content"] for m in messages if m["role"] == "user")

    assert "ROTEIRO_COMPLETO_XYZ" in user_prompt


# ── Testes de retry em _call_llm ──────────────────────────────────────────────


def test_retry_on_timeout_succeeds_on_third_attempt() -> None:
    """
    _call_llm deve retentar automaticamente em APITimeoutError e ter sucesso
    na 3ª tentativa. O sleep do tenacity é mockado para o teste ser instantâneo.
    """
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _timeout_error(),       # 1ª tentativa: timeout
        _timeout_error(),       # 2ª tentativa: timeout
        _fake_response("ok"),   # 3ª tentativa: sucesso
    ]

    with patch("time.sleep"):   # evita espera real do backoff exponencial
        result = _call_llm(client, system="sys", user="usr", step="test_timeout")

    assert result == "ok"
    assert client.chat.completions.create.call_count == 3


def test_retry_exhausted_reraises_timeout() -> None:
    """Após 3 timeouts consecutivos, APITimeoutError deve ser relançado."""
    client = MagicMock()
    client.chat.completions.create.side_effect = _timeout_error()

    with patch("time.sleep"), pytest.raises(APITimeoutError):
        _call_llm(client, system="sys", user="usr", step="test_exhaust")

    assert client.chat.completions.create.call_count == 3


def test_retry_on_empty_response() -> None:
    """
    _call_llm deve lançar EmptyResponseError internamente e retentar quando
    o Ollama responde com string vazia ou só espaços, e ter sucesso na 3ª tentativa.
    """
    client = MagicMock()
    empty = _fake_response("   ")       # só whitespace — deve disparar retry

    client.chat.completions.create.side_effect = [
        empty,
        empty,
        _fake_response("conteúdo válido"),
    ]

    with patch("time.sleep"):
        result = _call_llm(client, system="sys", user="usr", step="test_empty")

    assert result == "conteúdo válido"
    assert client.chat.completions.create.call_count == 3


def test_retry_on_none_content() -> None:
    """_call_llm deve retentar quando content é None (Ollama falhou internamente)."""
    client = MagicMock()
    none_resp = _fake_response("")          # string vazia também é vazio
    none_resp.choices[0].message.content = None

    client.chat.completions.create.side_effect = [
        none_resp,
        none_resp,
        _fake_response("resposta real"),
    ]

    with patch("time.sleep"):
        result = _call_llm(client, system="sys", user="usr", step="test_none")

    assert result == "resposta real"


def test_llm_response_is_stripped() -> None:
    """_call_llm deve remover whitespace extra das bordas da resposta."""
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response(
        "\n\n  Resposta com espaços  \n\n"
    )

    result = _call_llm(client, system="sys", user="usr", step="test_strip")

    assert result == "Resposta com espaços"


# ── Testes de save_script ─────────────────────────────────────────────────────


def test_save_script_creates_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """save_script deve criar storage/{channel_id}/{video_id}/script.txt."""
    monkeypatch.chdir(tmp_path)

    result = ScriptResult(
        angle="Ângulo de teste",
        outline="Outline de teste",
        full_script="Conteúdo completo do roteiro de teste",
        hook="Hook de teste",
        narration="Conteúdo completo do roteiro de teste",
    )

    path = save_script(result, channel_id=1, video_id=42)

    assert Path(path).exists()
    assert Path(path).read_text(encoding="utf-8") == "Conteúdo completo do roteiro de teste"
    # Caminho deve seguir a convenção storage/{channel_id}/{video_id}/script.txt
    assert "storage" in path
    assert "1" in path
    assert "42" in path
    assert path.endswith("script.txt")


def test_save_script_returns_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """save_script deve retornar um str (não Path)."""
    monkeypatch.chdir(tmp_path)

    result = ScriptResult(angle="a", outline="o", full_script="f", hook="h", narration="f")
    path = save_script(result, channel_id=99, video_id=1)

    assert isinstance(path, str)


def test_save_script_creates_intermediate_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_script deve criar diretórios intermediários automaticamente."""
    monkeypatch.chdir(tmp_path)

    result = ScriptResult(angle="a", outline="o", full_script="conteúdo", hook="h", narration="conteúdo")
    save_script(result, channel_id=777, video_id=999)

    expected_dir = tmp_path / "storage" / "777" / "999"
    assert expected_dir.is_dir()


def test_save_script_saves_full_script_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O arquivo script.txt deve conter exatamente o full_script do result."""
    monkeypatch.chdir(tmp_path)

    expected_text = "Este é o roteiro completo com acentuação: ção, ã, é."
    result = ScriptResult(
        angle="a",
        outline="o",
        full_script=expected_text,
        hook="h",
        narration="texto limpo",
    )

    path = save_script(result, channel_id=2, video_id=10)

    assert Path(path).read_text(encoding="utf-8") == expected_text


def test_save_script_also_saves_narration_txt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_script deve criar narration.txt além de script.txt."""
    monkeypatch.chdir(tmp_path)

    result = ScriptResult(
        angle="a",
        outline="o",
        full_script="**Seção:**\nTexto falado.",
        hook="h",
        narration="Texto falado.",
    )
    save_script(result, channel_id=3, video_id=5)

    narration_file = tmp_path / "storage" / "3" / "5" / "narration.txt"
    assert narration_file.exists()
    assert narration_file.read_text(encoding="utf-8") == "Texto falado."


# ── Testes de sanitize_llm_output ────────────────────────────────────────────


def test_sanitize_complete_think_block() -> None:
    """Padrão A: bloco <think>…</think> completo deve ser removido."""
    text = "<think>Vou planejar o roteiro cuidadosamente...</think>\nResposta real."
    result = sanitize_llm_output(text)
    assert "<think>" not in result
    assert "Vou planejar" not in result
    assert "Resposta real." in result


def test_sanitize_case_insensitive() -> None:
    """Tags <THINK> e <Think> também devem ser removidas."""
    text = "<THINK>raciocínio</THINK> Conteúdo."
    result = sanitize_llm_output(text)
    assert "raciocínio" not in result
    assert "Conteúdo." in result


def test_sanitize_multiple_complete_blocks() -> None:
    """Múltiplos blocos <think> em sequência devem ser todos removidos."""
    text = "<think>bloco 1</think> real <think>bloco 2</think> final"
    result = sanitize_llm_output(text)
    assert "bloco 1" not in result
    assert "bloco 2" not in result
    assert "real" in result
    assert "final" in result


def test_sanitize_clean_text_unchanged() -> None:
    """Texto limpo (sem think) deve ser retornado sem alteração."""
    text = "Texto normal sem raciocínio."
    assert sanitize_llm_output(text) == text


def test_sanitize_orphaned_closing_tag_real_qwen3_pattern() -> None:
    """
    Padrão B — padrão REAL do Qwen3 via Ollama:
    raciocínio sem <think> de abertura, terminando com </think>.
    Tudo antes de </think> deve ser descartado.
    """
    text = (
        "nowrap\n\n"
        "Okay, the user wants a YouTube script about sleep and memory.\n"
        "Let me think about the best angle for this...\n"
        "I should focus on the surprising aspect of memory consolidation.\n"
        "</think>\n\n"
        "O cérebro humano apaga memórias enquanto você dorme."
    )
    result = sanitize_llm_output(text)

    assert "nowrap" not in result
    assert "Okay, the user" not in result
    assert "Let me think" not in result
    assert "</think>" not in result
    assert "O cérebro humano apaga memórias" in result


def test_sanitize_orphaned_tag_no_nowrap() -> None:
    """Padrão B sem 'nowrap': raciocínio puro antes de </think>."""
    text = (
        "Analyzing the request...\n"
        "The topic is about sleep science.\n"
        "</think>\n\n"
        "Hoje vamos falar sobre como o sono afeta sua memória."
    )
    result = sanitize_llm_output(text)

    assert "Analyzing" not in result
    assert "</think>" not in result
    assert "Hoje vamos falar sobre" in result


def test_sanitize_nowrap_alone_no_think_tag() -> None:
    """'nowrap' sem </think> na sequência deve ser removido mesmo assim."""
    text = "nowrap\n\nConteúdo real em PT-BR."
    result = sanitize_llm_output(text)
    assert "nowrap" not in result
    assert "Conteúdo real em PT-BR." in result


def test_sanitize_mixed_complete_and_orphaned() -> None:
    """
    Padrão misto: bloco completo E raciocínio órfão no mesmo output.
    O Passo 1 remove o completo, o Passo 2 remove o orphan.
    """
    text = (
        "<think>pensamento rápido inicial</think>\n"
        "nowrap\n\n"
        "Mais raciocínio solto aqui...\n"
        "</think>\n\n"
        "Conteúdo final em português."
    )
    result = sanitize_llm_output(text)

    assert "pensamento rápido" not in result
    assert "nowrap" not in result
    assert "Mais raciocínio" not in result
    assert "</think>" not in result
    assert "Conteúdo final em português." in result


def test_sanitize_preserves_accented_portuguese() -> None:
    """Caracteres acentuados do PT-BR devem ser preservados intactos."""
    text = "A consolidação da memória acontece durante o sono profundo."
    assert sanitize_llm_output(text) == text


# alias: _strip_think_blocks aponta para sanitize_llm_output — testes de regressão
def test_strip_think_alias_works() -> None:
    """_strip_think_blocks é alias de sanitize_llm_output — deve se comportar igual."""
    text = "<think>raciocínio</think>\nConteúdo."
    assert _strip_think_blocks(text) == sanitize_llm_output(text)


# ── Testes de _call_llm com sanitize ─────────────────────────────────────────


def test_call_llm_sanitizes_complete_think_block() -> None:
    """_call_llm deve remover <think>…</think> antes de retornar a resposta."""
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response(
        "<think>Vou analisar o tema...</think>\n\nAnálise pronta: resposta real aqui."
    )

    result = _call_llm(client, system="sys", user="usr", step="test")

    assert "<think>" not in result
    assert "Vou analisar" not in result
    assert "Análise pronta: resposta real aqui." in result


def test_call_llm_sanitizes_real_qwen3_orphaned_pattern() -> None:
    """
    _call_llm deve limpar o padrão REAL do Qwen3 com </think> sem tag de abertura.
    """
    raw = (
        "nowrap\n\n"
        "Okay, the user wants a YouTube Short about sleep science.\n"
        "Let me structure this carefully for a PT-BR audience.\n"
        "</think>\n\n"
        "O sono é essencial para a memória."
    )
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response(raw)

    result = _call_llm(client, system="sys", user="usr", step="test")

    assert "nowrap" not in result
    assert "Okay, the user" not in result
    assert "</think>" not in result
    assert "O sono é essencial para a memória." in result


def test_call_llm_retries_when_response_is_only_think_block() -> None:
    """
    Se a resposta for APENAS um bloco <think> (sem conteúdo real),
    _call_llm deve lançar EmptyResponseError internamente e retentar.
    """
    client = MagicMock()
    only_think = _fake_response("<think>Só raciocínio, sem output real.</think>")
    real_response = _fake_response("<think>ok</think>\nConteúdo real gerado.")

    client.chat.completions.create.side_effect = [only_think, only_think, real_response]

    with patch("time.sleep"):
        result = _call_llm(client, system="sys", user="usr", step="test_think_retry")

    assert "Conteúdo real gerado." in result
    assert client.chat.completions.create.call_count == 3


def test_call_llm_passes_think_false_in_extra_body() -> None:
    """
    Camada 1: _call_llm deve passar extra_body={"think": False} para o Ollama,
    desabilitando o thinking mode no servidor antes de qualquer token ser gerado.
    """
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response("resposta ok")

    _call_llm(client, system="sys", user="usr", step="test")

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs.get("extra_body") == {"think": False}


def test_call_llm_system_prompt_contains_no_think_token() -> None:
    """
    Camada 1: todos os system prompts gerados devem começar com /no_think,
    o token que o Qwen3 reconhece para desabilitar o thinking mode.
    """
    client = _client_with("ângulo", "outline", "roteiro", "hook")

    generate_script(topic="teste", persona="teste", fmt="long", client=client)

    for i, call in enumerate(client.chat.completions.create.call_args_list):
        messages: list[dict[str, str]] = call.kwargs["messages"]
        system = next(m["content"] for m in messages if m["role"] == "system")
        assert system.startswith("/no_think"), (
            f"Etapa {i + 1}: system prompt não começa com /no_think"
        )


# ── Testes de _extract_narration ──────────────────────────────────────────────


def test_extract_narration_removes_emojis() -> None:
    """Emojis devem ser removidos da narração."""
    text = "Olha só 🧠 isso é incrível! 🚀 Vamos começar."
    result = _extract_narration(text)
    assert "🧠" not in result
    assert "🚀" not in result
    assert "Olha só" in result
    assert "isso é incrível" in result


def test_extract_narration_removes_bold_markdown() -> None:
    """**texto** deve virar 'texto' (asteriscos removidos, conteúdo mantido)."""
    text = "Isso é **muito importante** para entender."
    result = _extract_narration(text)
    assert "**" not in result
    assert "muito importante" in result


def test_extract_narration_removes_section_labels_with_timing() -> None:
    """Linhas como **Gancho (0-5s):** devem ser removidas completamente."""
    text = "**Gancho (0-5s):**\nO cérebro humano apaga memórias enquanto dorme."
    result = _extract_narration(text)
    assert "Gancho" not in result
    assert "0-5s" not in result
    assert "O cérebro humano apaga memórias" in result


def test_extract_narration_removes_markdown_headers() -> None:
    """Linhas começando com # devem ser removidas."""
    text = "## Seção 1 — Abertura\nAqui começa o conteúdo real."
    result = _extract_narration(text)
    assert "##" not in result
    assert "Seção 1" not in result
    assert "Aqui começa o conteúdo real." in result


def test_extract_narration_removes_solo_stage_directions() -> None:
    """Linhas que são APENAS direções de cena (*texto*) devem ser removidas."""
    text = "Hoje vamos falar sobre memória.\n*clique de relógio*\nO sono é essencial."
    result = _extract_narration(text)
    assert "clique de relógio" not in result
    assert "Hoje vamos falar sobre memória." in result
    assert "O sono é essencial." in result


def test_extract_narration_preserves_text_inside_single_asterisks() -> None:
    """
    Ênfase com asteriscos simples (*texto*) deve remover APENAS os marcadores,
    preservando a palavra interna na narração.

    Casos reais reportados:
      "fazendo uma *limpeza* para não"  → "fazendo uma limpeza para não"
      "Em outras palavras: *não vamos perder tempo*."
                                        → "Em outras palavras: não vamos perder tempo."
    """
    case1 = "fazendo uma *limpeza* para não"
    result1 = _extract_narration(case1)
    assert "limpeza" in result1, "palavra interna 'limpeza' foi apagada"
    assert "*" not in result1, "asterisco sobrou na saída"

    case2 = "Em outras palavras: *não vamos perder tempo*."
    result2 = _extract_narration(case2)
    assert "não vamos perder tempo" in result2, "frase interna foi apagada"
    assert "*" not in result2, "asterisco sobrou na saída"


def test_extract_narration_removes_think_blocks() -> None:
    """Blocos <think> devem ser removidos (segurança extra além do _call_llm)."""
    text = "<think>Vou planejar...</think>\nO cérebro apaga memórias enquanto dormimos."
    result = _extract_narration(text)
    assert "<think>" not in result
    assert "Vou planejar" not in result
    assert "O cérebro apaga memórias" in result


def test_extract_narration_removes_horizontal_rules() -> None:
    """Linhas de separação (---, ***) devem ser removidas."""
    text = "Primeira parte.\n---\nSegunda parte."
    result = _extract_narration(text)
    assert "---" not in result
    assert "Primeira parte." in result
    assert "Segunda parte." in result


def test_extract_narration_preserves_accented_portuguese() -> None:
    """Acentos do português (ção, ã, é, ú) devem ser preservados intactos."""
    text = "A consolidação da memória acontece durante o sono profundo."
    result = _extract_narration(text)
    assert result == text


def test_extract_narration_collapses_multiple_blank_lines() -> None:
    """Sequências de mais de 2 linhas em branco devem virar no máximo uma."""
    text = "Parágrafo 1.\n\n\n\nParágrafo 2."
    result = _extract_narration(text)
    assert "\n\n\n" not in result
    assert "Parágrafo 1." in result
    assert "Parágrafo 2." in result


# ── Testes de integração: campo narration no ScriptResult ────────────────────


def test_narration_field_exists_in_script_result() -> None:
    """ScriptResult deve ter o campo `narration` preenchido."""
    client = _client_with("ângulo", "outline", "roteiro limpo.", "hook")

    result = generate_script(
        topic="Sono e memória",
        persona="Divulgador científico",
        fmt="short",
        client=client,
    )

    assert hasattr(result, "narration")
    assert isinstance(result.narration, str)
    assert len(result.narration) > 0


def test_narration_has_no_emojis_or_markdown() -> None:
    """
    narration gerado a partir de full_script com emojis e rótulos de seção
    deve ser limpo.
    """
    dirty_script = (
        "**Gancho (0-5s):**\n"
        "🧠 O cérebro apaga memórias enquanto você dorme.\n"
        "---\n"
        "*pausa dramática*\n"
        "Isso acontece porque o sono REM é essencial para consolidar o aprendizado."
    )
    client = _client_with("ângulo", "outline", dirty_script, "hook")

    result = generate_script(
        topic="Sono e memória",
        persona="Cientista acessível",
        fmt="short",
        client=client,
    )

    assert "🧠" not in result.narration
    assert "**" not in result.narration
    assert "Gancho" not in result.narration
    assert "0-5s" not in result.narration
    assert "pausa dramática" not in result.narration
    assert "---" not in result.narration
    # Conteúdo real preservado
    assert "O cérebro apaga memórias" in result.narration
    assert "sono REM" in result.narration


def test_narration_differs_from_full_script_when_dirty() -> None:
    """narration e full_script devem ser diferentes quando o script tem formatação."""
    dirty_script = "**Seção (0-5s):**\n🚀 Texto real aqui."
    client = _client_with("ângulo", "outline", dirty_script, "hook")

    result = generate_script(
        topic="teste",
        persona="teste",
        fmt="short",
        client=client,
    )

    assert result.full_script == dirty_script
    assert result.narration != result.full_script


def test_language_rule_in_all_four_system_prompts() -> None:
    """
    O bloco IDIOMA – REGRA ABSOLUTA deve aparecer em todos os 4 system prompts,
    garantindo que o anti-espanhol se aplique a cada etapa.
    """
    client = _client_with("ângulo", "outline", "roteiro", "hook")

    generate_script(
        topic="Qualquer tema",
        persona="Qualquer persona",
        fmt="long",
        client=client,
    )

    for i, call in enumerate(client.chat.completions.create.call_args_list):
        messages: list[dict[str, str]] = call.kwargs["messages"]
        system_prompt = next(m["content"] for m in messages if m["role"] == "system")
        assert "REGRA ABSOLUTA" in system_prompt, (
            f"Regra de idioma ausente no system prompt da etapa {i + 1}"
        )


# ── Testes de integração: padrões REAIS do Qwen3 contaminando o pipeline ──────


_QWEN3_ORPHANED_PREFIX = (
    "nowrap\n\n"
    "Okay, the user wants a YouTube script about sleep science.\n"
    "Let me think carefully about the angle and structure.\n"
    "I should make it engaging for a PT-BR audience.\n"
    "</think>\n\n"
)


def test_full_script_clean_when_qwen3_emits_orphaned_think() -> None:
    """
    Quando TODAS as 4 etapas retornam o padrão real do Qwen3 (orphaned </think>),
    o full_script deve estar 100% livre de artefatos de raciocínio.
    """
    pt_content = {
        "angle": "O ângulo escolhido é a consolidação de memória durante o sono.",
        "outline": "1. Introdução\n2. Ciência\n3. Conclusão",
        "script": "O sono profundo é quando o cérebro consolida memórias do dia.",
        "hook": "Você sabia que o cérebro descarta memórias enquanto você dorme?",
    }

    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _fake_response(_QWEN3_ORPHANED_PREFIX + pt_content["angle"]),
        _fake_response(_QWEN3_ORPHANED_PREFIX + pt_content["outline"]),
        _fake_response(_QWEN3_ORPHANED_PREFIX + pt_content["script"]),
        _fake_response(_QWEN3_ORPHANED_PREFIX + pt_content["hook"]),
    ]

    result = generate_script(
        topic="sono e memória",
        persona="canal de ciência",
        fmt="short",
        client=client,
    )

    for field in ("angle", "outline", "full_script", "hook", "narration"):
        value: str = getattr(result, field)
        assert "nowrap" not in value, f"{field} contém 'nowrap'"
        assert "Okay, the user" not in value, f"{field} contém raciocínio em inglês"
        assert "</think>" not in value, f"{field} contém </think>"

    # Conteúdo PT-BR deve estar presente
    assert pt_content["script"] in result.full_script
    assert pt_content["hook"] in result.hook


def test_narration_clean_when_full_script_has_complete_think_block() -> None:
    """
    narration deve estar limpa mesmo que full_script tenha chegado com
    bloco <think>…</think> completo (verificação da Camada 2 em _extract_narration).
    """
    script_with_think = (
        "<think>Checking structure one more time...</think>\n\n"
        "**Gancho (0-5s):**\n"
        "O sono molda cada memória que você tem."
    )
    client = _client_with(
        "ângulo editorial",
        "outline aqui",
        script_with_think,
        "hook limpo",
    )

    result = generate_script(
        topic="sono",
        persona="ciência",
        fmt="short",
        client=client,
    )

    assert "<think>" not in result.narration
    assert "</think>" not in result.narration
    assert "Checking structure" not in result.narration
    assert "O sono molda cada memória" in result.narration
