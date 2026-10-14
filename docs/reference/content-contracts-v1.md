# Content Engine — contratos v1 (etapa 0)

Decididos com o proprietário em 2026-10-14. Este documento e os quatro módulos abaixo são a
fronteira combinada antes de as faixas de implementação começarem.

Estado em 2026-10-14, depois da junção da faixa A: o modelo de texto e o plano de cenas
estão ligados ao A1 e ao A3 (adapters em `text_model_ollama.py`, `text_model_codex.py` e
`text_model_factory.py`), com `ollama` como padrão. O timeline e o perfil de canal ainda não
estão ligados a nada.

| Contrato | Código | Testes |
| --- | --- | --- |
| Modelo de texto | `src/pipeline/text_model.py` | `tests/pipeline/test_text_model.py` |
| Plano de cenas (A3) | `src/pipeline/scene_plan.py` | `tests/pipeline/test_scene_plan.py` |
| Timeline (A5) | `src/pipeline/timeline.py` | `tests/pipeline/test_timeline.py` |
| Perfil de canal | `src/pipeline/channel_profile.py` | `tests/pipeline/test_channel_profile.py` |

## Quem escreve o quê

| Informação | Quem decide |
| --- | --- |
| Texto do roteiro, descrição de cena, personagens e cenários | modelo de texto |
| Quantidade de imagens e trecho da narração de cada uma | código (`visual_count_bounds`, `plan_visual_slots`) |
| Tempos de corte, duração das imagens e tempo de cada palavra | código, a partir do WAV (A2) e das legendas (A4) |
| Efeito e transição de cada imagem | código, por regra determinística |
| Nicho, formatos, estilo e frequência do canal | proprietário, no perfil do canal |

O modelo de texto nunca escreve tempos nem decide quantidade.

## 1. Modelo de texto

Interface única para A1 (roteiro), A3 (plano de cenas e reescrita a partir do comentário do
revisor) e, depois, sugestão de nicho.

- `TextModelRequest(purpose, system, user, response_schema=None)`. `purpose` é um rótulo
  `snake_case` usado em logs e métricas. `response_schema` pede resposta JSON com aquele JSON
  Schema; o chamador valida a resposta mesmo assim.
- `TextModelResult(text, provider, model_id, duration_seconds, fallback_from)`. Resposta vazia
  é inválida por construção.
- Três erros: `TextModelRetryableError` (timeout, conexão, resposta vazia),
  `TextModelQuotaError` (subclasse do anterior: limite de uso do plano atingido) e
  `TextModelPermanentError` (configuração ou login ausente).
- `FallbackTextModel(primary, fallback)` só passa o pedido ao reserva com
  `TextModelQuotaError`. Timeout ou configuração quebrada não trocam de modelo em silêncio.
  Quando o reserva responde, `fallback_from` registra quem foi pedido primeiro.
- `as_scene_model_call(model, purpose=...)` adapta a interface ao `SceneModelCall` que
  `visual_prompts.py` já usa.

Providers previstos (`TEXT_MODEL_PROVIDERS`):

- `ollama`: o adapter atual (`qwen3:14b`), sempre pela API nativa com contexto fixo.
  Continua sendo o padrão e o reserva.
- `codex_exec`: o plano do ChatGPT do proprietário via `codex exec`, que a documentação da
  OpenAI prevê para scripts com login do ChatGPT. Chamada implementada
  (`src/pipeline/text_model_codex.py`, Codex CLI 0.159.3):

  ```text
  codex exec --sandbox read-only --skip-git-repo-check --ephemeral --ignore-rules
             --color never --json -C <pasta temporária vazia> [-m <modelo>]
             [--output-schema <arquivo>] -o <arquivo da resposta> -
  ```

  O prompt vai pelo stdin e a resposta é lida do arquivo `-o`. A pasta vazia impede que o
  agente leia o projeto ou o `AGENTS.md`. O processo nunca recebe credencial pelo pedido, e
  prompt e resposta não vão para log. Diferenças em relação à chamada combinada na etapa 0:

  - `--json` e `--color never`: as falhas são lidas dos eventos `error` e `turn.failed`, nunca
    da saída do console, que repete o prompt.
  - Antes de cada pedido, `codex login status` precisa informar login com ChatGPT; sem isso a
    chamada falha como `TextModelPermanentError`. `OPENAI_API_KEY` e `CODEX_API_KEY` são
    removidas do ambiente do processo, para que nenhuma chamada seja cobrada na API.
  - No Windows, o atalho `codex.cmd` do npm é trocado pelo binário nativo, porque o atalho
    exige `cmd.exe` e deixaria o processo real rodando depois de um timeout.
  - `/no_think` (opção do Qwen3) é retirado das instruções enviadas ao Codex.

  Verificado em 2026-10-14, com autorização do proprietário, por um teste real opt-in
  (`tests/integration/test_codex_exec_text_model.py`): resposta em 19 s com login ChatGPT, e
  `--output-schema` aceitou o schema do plano de cenas. **Ainda não confirmado:** o texto que
  o Codex usa ao atingir o limite de uso. Hoje ele é reconhecido por padrão (`usage limit`,
  `rate limit`, `quota`, `429`) só nos eventos de erro; até ver um limite real, a troca para o
  reserva não foi observada. Também falta confirmar que o runtime, que roda como tarefa
  agendada, enxerga o login do Codex do usuário.

O plano do Claude não é usado pela pipeline: a documentação do Agent SDK proíbe produtos de
terceiros de usar login do claude.ai, e o proprietário reserva esse plano para
desenvolvimento.

## 2. Plano de cenas (A3)

Substitui a descrição isolada de cada imagem por um plano do vídeo inteiro numa só chamada.

```json
{
  "schema_version": 1,
  "characters": [{"id": "student", "description": "A young adult with short curly hair ..."}],
  "settings": [{"id": "bedroom_desk", "description": "A small bedroom with a wooden desk ..."}],
  "scenes": [
    {"index": 1, "description": "A person drops loose pens into a ceramic cup ...",
     "setting_id": "bedroom_desk", "character_ids": ["student"], "shot": "medium"}
  ]
}
```

- `parse_scene_plan(resposta, scene_count=n)` exige exatamente `n` cenas numeradas de 1 a
  `n`; o `n` vem dos trechos calculados pelo código. Recusas levantam `ScenePlanError` com um
  motivo curto em inglês, que pode ser reenviado ao modelo como hoje.
- Toda descrição (cena, personagem e cenário) passa por `scene_prompt_problem`: tamanho,
  inglês e as palavras proibidas (texto, logotipo, tablet, TV, notificação, celebridade...).
  Desde 2026-10-14, a pedido do proprietário depois da run 18 (um roteiro sobre mensagens
  ilustrado com folhas em branco), celular, notebook e computador são aceitos, mas só quando
  a mesma descrição diz que a tela está virada, desligada ou fora de foco ("face down",
  "screen turned away", "screen is off", "out of focus", "closed laptop"...). A checagem é
  por palavras e não confere a imagem gerada; a revisão humana continua obrigatória.
- Proibido: id repetido, referência a personagem ou cenário inexistente, descrição de cena
  repetida e duas cenas seguidas com o mesmo cenário **e** o mesmo enquadramento (`shot`).
- As cenas usam pelo menos 3 cenários diferentes (um por cena quando há menos de 3 cenas).
  Na run 18 as nove imagens ficaram na mesma mesa.
- Limites: até 4 personagens, 8 cenários, 3 personagens por cena e 60 cenas.
- `scene_image_prompt(plano, i)` monta o texto da imagem `i` repetindo a descrição do
  personagem e do cenário, o que mantém a mesma figura e o mesmo lugar reconhecíveis. O
  estilo visual do vídeo continua sendo somado por `styled_prompt`.

## 3. Timeline (A5)

Tudo o que um motor de render precisa para desenhar um vídeo. O mesmo arquivo serve ao
FFmpeg e ao Remotion. Tempos em **milissegundos inteiros**, para Python e JavaScript lerem os
mesmos valores.

- JSON Schema versionado: `docs/reference/schemas/timeline-v1.schema.json`. Exemplo válido:
  `docs/reference/schemas/timeline-v1.example.json`. Um teste falha se o schema commitado
  divergir do modelo.
- Campos principais: `format` (`short` 1080×1920 ou `long` 1920×1080), `fps` 30,
  `duration_ms`, `audio_sha256`, `caption_ass_sha256`, `caption_template`, `transition_ms`,
  `clips[]` e `words[]`.
- `clips`: numerados em ordem, contíguos, do zero ao fim do áudio, cada um com pelo menos
  1 s; `effect` ∈ `none`, `zoom_in`, `zoom_out`, `pan_left`, `pan_right`; `transition_in` ∈
  `cut`, `crossfade`. O primeiro sempre começa com corte, e um crossfade não passa de metade
  do clipe mais curto.
- `words`: palavras em ordem, sem sobreposição, dentro do áudio. Elas são extraídas do ASS
  verificado do A4 (cujo hash o timeline carrega), nunca de outra transcrição. Assim o gate de
  alinhamento e o A7 continuam conferindo a mesma evidência.
- `caption_template`: `phrase_blocks` (o visual atual do A4) ou `word_pop` (uma palavra grande
  por vez, estilo "Reddit stories").
- `timeline_to_json` produz JSON canônico (mesmo timeline, mesmos bytes) para hash e
  artifact.

## 4. Perfil de canal

Validação do que um canal guarda; persistência fica para a etapa 5.

- `slug`, `name`, `language` (só `pt-BR` por enquanto), `niche`, `persona` (mesmos limites da
  persona do A1), `formats` (`short`, `long` ou `both`), `visual_style`,
  `caption_templates` (um por formato publicado, exatamente), `voice_reference_id`
  (identificador da gravação de referência, nunca caminho), `videos_per_day` (0 a 4; 0 =
  só disparo manual) e `status` (`draft`, `active`, `paused`, `archived`; nasce `draft`).
- Sem segredo nem caminho de arquivo. A tabela legada `channels` não é reutilizada porque
  mistura o perfil com credenciais de publicação, que continuam fora do escopo.
- Na etapa 5: tabela própria do Content Engine com migration, vínculo da run com o canal e
  janela da similaridade (A6) restrita ao canal.

## Fora destes contratos

- Licença: o escopo `quality-test-only` de imagens e voz continua valendo até a decisão
  escrita do proprietário sobre os dados de treino (ver `docs/licensing/`).
- Upload, publicação e sugestão de nicho com dados reais do YouTube.
