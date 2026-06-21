# Arquitetura & Guia de Desenvolvimento
## Plataforma de Automação de Conteúdo + Dashboard de Controle (com Cursor AI)

**Versão:** 1.0 — 2026
**Stack-alvo:** Python 3.12 · FastAPI · Celery · Redis · SQLite/Postgres · IA local (RTX 3090)
**Modelo de trabalho:** Você define a arquitetura (este documento). O Cursor escreve o código, módulo a módulo, seguindo esta especificação.

---

## 0. Como usar este documento

Este documento é a **fonte da verdade** da arquitetura. O fluxo de trabalho é:

1. **Coloque este arquivo (`ARQUITETURA.md`) e o `AGENTS.md` na raiz do repositório.** O Cursor lê os dois automaticamente como contexto.
2. **Desenvolva um módulo por vez**, na ordem do roadmap (Seção 9). Não peça ao Cursor para "construir tudo" — isso gera código que você não entende e não consegue depurar.
3. **Para cada módulo:** abra o chat do Cursor, cole o **Prompt** da seção daquele módulo (ele já referencia este documento), revise o código gerado, e só então rode o **Checklist de checkpoint**.
4. **Só avance de módulo quando o checkpoint passar.** Cada checkpoint é um portão (gate). Se não passou, o módulo não está pronto, por mais que "pareça funcionar".
5. **Você é o arquiteto e o revisor.** O Cursor erra — principalmente em integração entre módulos, tratamento de erro e licenças. Sua revisão é a camada de qualidade.

> **Princípio central:** o sistema é uma fábrica modular onde a IA faz o trabalho braçal e **você decide tópico, ângulo, thumbnail e variação**. Essa camada editorial humana não é opcional — é o que mantém os canais monetizados sob a política de "conteúdo inautêntico" do YouTube (ver Seção 10).

---

## 1. Visão geral da arquitetura

O sistema tem **quatro planos** (layers):

```
┌─────────────────────────────────────────────────────────────────────┐
│  PLANO DE APRESENTAÇÃO — Dashboard (FastAPI + HTMX/React)             │
│  Monitora tudo, controla o pipeline, mostra receita/saúde/alertas    │
└───────────────┬──────────────────────────────────┬──────────────────┘
                │ lê (DB + APIs)                    │ escreve comandos
                ▼                                   ▼ (flags via Redis)
┌─────────────────────────────────────────────────────────────────────┐
│  PLANO DE CONTROLE — n8n (orquestração visual) + Celery Beat (cron)  │
│  Dispara runs, agenda publicações, coordena os blocos                │
└───────────────┬─────────────────────────────────────────────────────┘
                │ enfileira tarefas
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PLANO DE EXECUÇÃO — Celery Workers + Redis (broker)                 │
│  ┌────────────┐ ┌──────────────────┐ ┌──────────────────────────┐   │
│  │ Worker GPU │ │ Worker CPU       │ │ Worker IO                │   │
│  │ (conc.=1)  │ │ (FFmpeg, multi)  │ │ (APIs, scraping, upload) │   │
│  │ LLM/TTS/   │ │                  │ │                          │   │
│  │ SD/Whisper │ │                  │ │                          │   │
│  └────────────┘ └──────────────────┘ └──────────────────────────┘   │
└───────────────┬─────────────────────────────────────────────────────┘
                │ persiste estado + artefatos
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PLANO DE DADOS — SQLite (→ Postgres) + filesystem (/storage)        │
│  Metadados de vídeos, jobs, métricas, canais, tendências, etc.       │
└─────────────────────────────────────────────────────────────────────┘
```

**Regra de ouro da separação GPU:** a RTX 3090 é um recurso único e disputado. Toda tarefa que usa GPU (LLM, TTS, geração de imagem, Whisper) vai para a fila `gpu` com **concorrência = 1** (serializada). Tarefas de CPU (FFmpeg) e de rede (uploads, scraping, APIs) rodam em paralelo em workers separados. Nunca rode dois modelos pesados na GPU ao mesmo tempo.

---

## 2. Stack tecnológica (definida — não deixe o Cursor escolher)

| Camada | Tecnologia | Por quê |
|---|---|---|
| Linguagem | Python 3.12 | Compatibilidade com libs de IA |
| API/Backend | FastAPI (async) | Performance, tipagem, docs automáticas |
| Fila de tarefas | Celery + Redis | Maduro, escalável, filas múltiplas |
| Orquestração visual | n8n (Docker) | Control plane visual sobre o Celery |
| Banco | SQLite → PostgreSQL | Comece simples; migre se precisar de concorrência |
| ORM | SQLAlchemy 2.0 + Alembic | Tipado; migrações versionadas |
| Frontend | HTMX + Jinja2 (ou React) | HTMX = rápido de construir; React se quiser SPA |
| LLM local | Ollama (Qwen3 / Gemma) | API OpenAI-compatível, licença permissiva |
| TTS | Kokoro-82M (Apache 2.0) | Comercial livre, PT-BR, rápido |
| Imagem | SDXL ou FLUX Schnell (Apache 2.0) | Comercial livre (FLUX **Dev** não!) |
| Legendas | faster-whisper / WhisperX | Timing palavra-a-palavra |
| Montagem | FFmpeg | Padrão da indústria |
| Containers | Docker + docker-compose | Reprodutibilidade |
| Testes | pytest + pytest-asyncio | Padrão Python |
| Embeddings (similaridade) | sentence-transformers (local) | Roda na sua GPU, sem custo de API |

**Licenças que o Cursor costuma ignorar — você fiscaliza:**
- TTS: use **Kokoro** ou **Fish Speech** (Apache 2.0). **NÃO** use XTTS v2 / F5-TTS para conteúdo monetizado (não-comerciais).
- Imagem: use **SDXL** ou **FLUX Schnell** (Apache 2.0). **NÃO** use **FLUX Dev** (proíbe vender o output).

---

## 3. Estrutura do monorepo

```
content-engine/
├── AGENTS.md                  # Regras para o Cursor (Seção 5)
├── ARQUITETURA.md             # Este documento
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── alembic/                   # Migrações de banco
├── storage/                   # Artefatos (gitignored): scripts, áudio, vídeo
│   └── {channel_id}/{video_id}/
├── src/
│   ├── core/
│   │   ├── config.py          # Settings (pydantic-settings)
│   │   ├── database.py        # Engine, session, base
│   │   ├── models.py          # SQLAlchemy models (Seção 4)
│   │   └── celery_app.py      # Config Celery + filas
│   ├── pipeline/              # BLOCO A — núcleo
│   │   ├── script_gen.py      # A1
│   │   ├── tts.py             # A2
│   │   ├── visuals.py         # A3
│   │   ├── captions.py        # A4
│   │   ├── assembly.py        # A5
│   │   └── upload.py          # A6
│   ├── intelligence/          # BLOCO D
│   │   ├── trends.py          # D1
│   │   ├── competitors.py     # D2
│   │   └── similarity.py      # D3
│   ├── revenue/               # BLOCO E
│   │   ├── aggregator.py      # E1
│   │   ├── profit.py          # E2
│   │   └── ab_testing.py      # E3
│   ├── operations/            # BLOCO F
│   │   ├── health.py          # F1
│   │   ├── repurpose.py       # F2
│   │   └── seo.py             # F3
│   ├── engagement/            # BLOCO G
│   │   └── comments.py        # G1
│   ├── extras/                # BLOCO H (com ressalvas)
│   │   ├── x_bot.py           # H1
│   │   ├── affiliate.py       # H2
│   │   └── outreach.py        # H3
│   ├── tasks/                 # Tarefas Celery (wrappers dos módulos)
│   │   └── *.py
│   └── dashboard/             # BLOCO C
│       ├── main.py            # App FastAPI
│       ├── routers/
│       └── templates/         # Se HTMX
└── tests/
    └── ... (espelha src/)
```

---

## 4. Schema do banco de dados

Modele com SQLAlchemy 2.0 (estilo declarativo tipado). Tabelas principais:

```sql
-- Canais
channels (
  id            INTEGER PK,
  name          TEXT,
  niche         TEXT,
  platform      TEXT,        -- 'youtube' | 'tiktok'
  language      TEXT,        -- 'pt-BR' | 'en'
  persona       TEXT,        -- voz/identidade editorial do canal
  cloud_project_id TEXT,     -- projeto Google Cloud (cota de upload)
  oauth_refresh_token TEXT,  -- CRIPTOGRAFADO
  status        TEXT,        -- 'active' | 'paused' | 'flagged'
  created_at    TIMESTAMP
)

-- Vídeos
videos (
  id            INTEGER PK,
  channel_id    INTEGER FK,
  title         TEXT,
  description   TEXT,
  niche_angle   TEXT,        -- ângulo editorial específico (humano)
  topic_source  TEXT,        -- de onde veio a pauta (trends/competitor/manual)
  format        TEXT,        -- 'short' | 'long'
  script_path   TEXT,
  audio_path    TEXT,
  video_path    TEXT,
  thumbnail_path TEXT,
  platform_video_id TEXT,    -- ID no YouTube/TikTok após upload
  ai_disclosure BOOLEAN,     -- precisa de rótulo de IA?
  similarity_score REAL,     -- D3: quão parecido com vídeos recentes
  status        TEXT,        -- draft|queued|rendering|ready|scheduled|published|failed
  scheduled_at  TIMESTAMP,
  published_at  TIMESTAMP,
  created_at    TIMESTAMP
)

-- Jobs (rastreio fino de cada etapa do pipeline)
jobs (
  id            INTEGER PK,
  video_id      INTEGER FK,
  stage         TEXT,        -- script|tts|visual|caption|render|upload
  status        TEXT,        -- queued|running|done|failed
  worker        TEXT,
  celery_task_id TEXT,
  error_msg     TEXT,
  retries       INTEGER,
  started_at    TIMESTAMP,
  finished_at   TIMESTAMP
)

-- Métricas (uma linha por vídeo por dia)
metrics (
  id            INTEGER PK,
  video_id      INTEGER FK,
  date          DATE,
  views         INTEGER,
  watch_time_min INTEGER,
  avg_view_duration REAL,
  ctr           REAL,        -- click-through rate (E3)
  estimated_revenue REAL,
  cpm           REAL,
  monetized_playbacks INTEGER,
  subscribers_gained INTEGER,
  collected_at  TIMESTAMP
)

-- Custos (E2 — lucro real por vídeo)
costs (
  id            INTEGER PK,
  video_id      INTEGER FK,
  gpu_seconds   REAL,        -- tempo de GPU consumido
  kwh_estimated REAL,        -- energia estimada
  api_cost      REAL,        -- custo de APIs pagas (se houver)
  computed_cost REAL,        -- custo total em R$
  created_at    TIMESTAMP
)

-- Tendências/pautas (D1, D2)
topics (
  id            INTEGER PK,
  channel_id    INTEGER FK,  -- nullable (pode ser geral do nicho)
  source        TEXT,        -- 'google_trends'|'youtube'|'reddit'|'competitor'
  title         TEXT,
  raw_data      JSON,
  potential_score REAL,      -- score calculado de potencial
  status        TEXT,        -- 'new'|'approved'|'rejected'|'used'
  created_at    TIMESTAMP
)

-- Alertas (F1 — health monitor)
alerts (
  id            INTEGER PK,
  channel_id    INTEGER FK,
  type          TEXT,        -- 'strike'|'rpm_drop'|'demonetized'|'job_failed'
  severity      TEXT,        -- 'info'|'warning'|'critical'
  message       TEXT,
  acknowledged  BOOLEAN,
  created_at    TIMESTAMP
)

-- Variantes A/B (E3)
ab_variants (
  id            INTEGER PK,
  video_id      INTEGER FK,
  variant_type  TEXT,        -- 'thumbnail'|'title'
  content       TEXT,        -- caminho da thumb ou texto do título
  impressions   INTEGER,
  clicks        INTEGER,
  ctr           REAL,
  is_winner     BOOLEAN,
  created_at    TIMESTAMP
)
```

Índices: `videos(channel_id, status)`, `jobs(video_id, stage)`, `metrics(video_id, date)`, `topics(channel_id, status)`, `alerts(channel_id, acknowledged)`.

---

## 5. Configuração do Cursor (`AGENTS.md`)

Crie um arquivo `AGENTS.md` na raiz com estas regras. O Cursor (e a maioria dos agentes de código) lê esse arquivo automaticamente. Conteúdo recomendado:

```markdown
# Regras do projeto (para agentes de código)

## Contexto
Leia ARQUITETURA.md antes de qualquer tarefa. Ele é a fonte da verdade.

## Stack fixa (não substitua)
Python 3.12, FastAPI, Celery+Redis, SQLAlchemy 2.0, Ollama, Kokoro TTS,
SDXL/FLUX Schnell, faster-whisper, FFmpeg. NÃO troque essas escolhas.

## Convenções
- Type hints obrigatórios em toda função.
- Toda função que pode falhar (rede, GPU, IO) usa try/except com log estruturado.
- Tarefas Celery: idempotentes, com soft_time_limit e retries.
- Nada de segredos hardcoded. Tudo via src/core/config.py (pydantic-settings).
- Toda etapa GPU vai para a fila 'gpu' (concorrência 1).
- Escreva testes pytest junto com o código. Não entregue módulo sem teste.

## Licenças (CRÍTICO)
- TTS: só Kokoro ou Fish Speech (Apache 2.0). NUNCA XTTS/F5-TTS para monetização.
- Imagem: só SDXL ou FLUX Schnell. NUNCA FLUX Dev (proíbe venda do output).

## O que NÃO fazer
- Não criar pipeline "do tópico ao upload" sem ponto de revisão humana.
- Não gerar código que poste em massa sem variação (risco de ban).
- Não usar gpt4free nem APIs não-oficiais.
```

**Dicas de uso do Cursor:**
- Use `@ARQUITETURA.md` e `@arquivo.py` nos prompts para dar contexto explícito.
- Trabalhe em **um arquivo/módulo por conversa**. Conversas longas degradam a qualidade.
- Peça sempre: *"escreva os testes pytest junto"*. Code sem teste não passa no checkpoint.
- Quando integrar dois módulos, abra ambos no contexto e descreva o contrato (inputs/outputs) explicitamente.
- Revise **você mesmo** o tratamento de erro e as chamadas de API externas — é onde o Cursor mais erra.

---

# BLOCO A — Núcleo do pipeline

> Cada módulo abaixo segue o mesmo formato: **Objetivo · Responsabilidades · Entradas/Saídas · Prompt para o Cursor · Checklist de checkpoint.**

## A1 — Geração de roteiro (`pipeline/script_gen.py`)

**Objetivo:** transformar uma pauta (topic) em um roteiro estruturado, via LLM local (Ollama).

**Responsabilidades:**
- Receber `topic` + `channel.persona` + `format` (short/long).
- Gerar em etapas: ângulo → outline → roteiro por seção → hook isolado.
- Forçar variação estrutural (não repetir a abertura/estrutura de vídeos recentes).
- Salvar o roteiro em `storage/{channel_id}/{video_id}/script.txt` e retornar o caminho.

**Entradas/Saídas:**
- In: `topic_id`, `channel_id`, `format`.
- Out: `script_path`, texto do hook (para thumbnail/título depois).

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção A1) e @src/core/config.py.

Implemente src/pipeline/script_gen.py com uma função:

    def generate_script(topic: str, persona: str, fmt: Literal["short","long"]) -> ScriptResult

Use o cliente OpenAI (openai lib) apontando para o Ollama local:
base_url="http://localhost:11434/v1", api_key="ollama", model configurável via config.

Gere em 4 chamadas encadeadas: (1) ângulo editorial específico, (2) outline,
(3) roteiro completo seção por seção, (4) hook dos primeiros 15-30s separado.

Inclua um parâmetro opcional `recent_openings: list[str]` e instrua o modelo,
via system prompt, a NÃO repetir nenhuma dessas aberturas (variação anti-template).

ScriptResult deve ser um dataclass/pydantic com: angle, outline, full_script, hook.
Trate timeouts e respostas vazias do Ollama com retry (3x, backoff).
Escreva testes em tests/pipeline/test_script_gen.py mockando o cliente Ollama.
NÃO chame o Ollama de verdade nos testes.
```

**Checklist de checkpoint A1:**
- [ ] `generate_script` roda com Ollama ligado e retorna `ScriptResult` preenchido.
- [ ] As 4 etapas aparecem separadas (ângulo, outline, roteiro, hook).
- [ ] Passar `recent_openings` muda visivelmente a abertura gerada.
- [ ] Timeout do Ollama dispara retry, não crash.
- [ ] Testes passam com cliente mockado (sem chamar Ollama real).
- [ ] O roteiro é salvo no caminho correto em `storage/`.

---

## A2 — Text-to-Speech (`pipeline/tts.py`)

**Objetivo:** converter o roteiro em narração (áudio), com voz consistente por canal.

**Responsabilidades:**
- Usar **Kokoro-82M** (Apache 2.0). Voz/idioma configurável por canal (PT-BR = `'p'`).
- Rodar na fila `gpu`.
- Salvar `audio_path` em `storage/...` e retornar duração total (para timing posterior).

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção A2). Implemente src/pipeline/tts.py:

    def synthesize(script_text: str, voice: str, lang_code: str = "p") -> TTSResult

Use Kokoro-82M (pip install kokoro). NÃO use XTTS nem F5-TTS (licença não-comercial).
Quebre o texto em sentenças, sintetize cada uma, concatene em um WAV/MP3 único via
ffmpeg ou pydub. Retorne TTSResult com audio_path e duration_seconds.
Mantenha o modelo carregado em memória entre chamadas (singleton) para não recarregar pesos.
Trate textos longos (chunking) e caracteres especiais do PT-BR.
Escreva teste que sintetiza uma frase curta e verifica que o arquivo existe e tem duração > 0.
```

**Checklist de checkpoint A2:**
- [ ] Gera áudio audível e correto em PT-BR a partir de um roteiro.
- [ ] A voz é a mesma entre execuções do mesmo canal (consistência).
- [ ] `duration_seconds` bate com a duração real do arquivo.
- [ ] Roda na fila `gpu` (não concorre com outra tarefa GPU).
- [ ] Confirmado: usa Kokoro, não XTTS/F5-TTS.
- [ ] Texto longo (>2 min) não estoura memória nem corta no fim.

---

## A3 — Visuais (`pipeline/visuals.py`)

**Objetivo:** gerar/obter os visuais do vídeo (imagens IA + B-roll de stock).

**Responsabilidades:**
- Imagens via **SDXL** ou **FLUX Schnell** (Apache 2.0) na fila `gpu`.
- B-roll via **Pexels/Pixabay API** (uso comercial livre; baixar para `storage/`, sem hotlinking).
- Decidir quantos visuais por duração de áudio (ex.: 1 a cada ~5-8s).

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção A3). Implemente src/pipeline/visuals.py com duas estratégias:

    def generate_images(prompts: list[str], out_dir: str) -> list[str]   # SDXL/FLUX Schnell local
    def fetch_stock(query: str, count: int, out_dir: str) -> list[str]    # Pexels/Pixabay API

Para geração local, use diffusers com SDXL ou FLUX Schnell (NUNCA FLUX Dev — proíbe venda).
Para stock, use as APIs Pexels e Pixabay (chaves via config). Baixe os arquivos para
out_dir (NÃO hotlink — o Pixabay proíbe). Respeite rate limits com backoff.
Uma função orquestradora visuals_for_video(script, audio_duration, mode) decide a
quantidade de visuais e chama a estratégia escolhida.
Escreva testes mockando as APIs de stock; para geração local, um teste marcado @gpu
que pode ser pulado em CI.
```

**Checklist de checkpoint A3:**
- [ ] `fetch_stock` baixa arquivos reais para `storage/` (não URLs).
- [ ] `generate_images` produz imagens coerentes com os prompts.
- [ ] Confirmado: usa SDXL ou FLUX **Schnell**, nunca FLUX Dev.
- [ ] Rate limit das APIs tratado (não toma 429).
- [ ] Número de visuais é proporcional à duração do áudio.
- [ ] Geração local roda na fila `gpu`.

---

## A4 — Legendas (`pipeline/captions.py`)

**Objetivo:** gerar legendas com timing palavra-a-palavra a partir do áudio.

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção A4). Implemente src/pipeline/captions.py:

    def transcribe_to_subtitles(audio_path: str, out_dir: str, lang: str = "pt") -> str

Use faster-whisper (modelo large-v3) com word_timestamps=True. Gere um arquivo .ass
estilizado (legenda estilo Shorts, palavra destacada). Retorne o caminho do .ass.
Roda na fila gpu. Escreva teste com um áudio curto de exemplo verificando que o .ass
tem timestamps válidos e não-sobrepostos.
```

**Checklist de checkpoint A4:**
- [ ] Transcrição em PT-BR precisa (>90% das palavras corretas num teste real).
- [ ] Timestamps palavra-a-palavra, sem sobreposição.
- [ ] Arquivo `.ass` válido e estilizado.
- [ ] Roda na fila `gpu`.

---

## A5 — Montagem (`pipeline/assembly.py`)

**Objetivo:** combinar áudio + visuais + legendas em um vídeo final via FFmpeg.

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção A5). Implemente src/pipeline/assembly.py:

    def assemble_video(audio_path, visuals: list[str], subtitle_path, fmt, out_path) -> str

Use FFmpeg (via subprocess, NÃO moviepy — performance). Para 'short': formato 9:16.
Para 'long': 16:9. Aplique efeito Ken Burns nas imagens estáticas, transições suaves,
queime as legendas .ass (-vf subtitles=). Sincronize a duração total dos visuais com a
do áudio. Retorne out_path. Esta tarefa é CPU-bound: roda na fila 'cpu' (paraleliza).
Trate erros do FFmpeg lendo stderr. Escreva teste que monta um vídeo de 5s com 1 imagem
+ 1 áudio + 1 legenda e verifica que o MP4 resultante é válido (ffprobe).
```

**Checklist de checkpoint A5:**
- [ ] Gera MP4 válido com áudio, visuais e legendas sincronizados.
- [ ] `short` sai 9:16, `long` sai 16:9.
- [ ] Duração do vídeo = duração do áudio (sem cortes/sobras).
- [ ] Roda na fila `cpu` (não bloqueia a GPU).
- [ ] Erro de FFmpeg vira exceção tratada com a mensagem do stderr.

---

## A6 — Upload (`pipeline/upload.py`)

**Objetivo:** publicar o vídeo no YouTube (e, com ressalvas, TikTok).

**Responsabilidades:**
- YouTube Data API v3 (`videos.insert`, OAuth 2.0, refresh token por canal).
- Respeitar cota: `videos.insert` = 1.600 unidades; 10.000/dia ⇒ ~6 uploads/dia/projeto.
- Aplicar rótulo de IA quando `ai_disclosure=True`.
- TikTok: implementar **apenas** se a Content Posting API estiver auditada; senão, gerar rascunho/inbox e avisar.

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção A6). Implemente src/pipeline/upload.py:

    def upload_youtube(video_path, title, description, tags, channel_id,
                       ai_disclosure: bool, privacy="private") -> str

Use google-api-python-client + OAuth2 (refresh token do canal, vindo do DB criptografado).
Faça upload resumável. Se ai_disclosure=True, marque o atributo de conteúdo alterado/sintético.
Comece com privacy='private' (revisão humana antes de publicar). Retorne platform_video_id.
Trate erro de cota (403 quotaExceeded) de forma explícita, sem retry cego.

Para TikTok, crie upload_tiktok() mas com um guard: se a flag config.TIKTOK_API_AUDITED
for False, NÃO tente postar — apenas registre que precisa de upload manual. Documente no
docstring que a API oficial do TikTok proíbe ferramentas de autouso.

Escreva testes mockando os clientes das APIs (não suba vídeo de verdade).
```

**Checklist de checkpoint A6:**
- [ ] Upload no YouTube funciona com OAuth real (teste manual com 1 vídeo `private`).
- [ ] Vídeo sobe como `private` por padrão (não publica direto).
- [ ] `ai_disclosure=True` aplica o rótulo de IA.
- [ ] Erro de cota (403) é tratado e logado, sem retry infinito.
- [ ] TikTok não tenta postar quando a API não está auditada.
- [ ] Testes passam com clientes mockados.

> **Checkpoint de bloco A (integração):** com A1–A6 prontos, monte uma `chain` Celery que produz **um vídeo de ponta a ponta** a partir de um `topic_id`, com revisão humana antes do upload. Esse é o marco da **Fase 0**.

---

# BLOCO B — Orquestração

## B1 — Celery + filas (`core/celery_app.py`, `tasks/`)

**Objetivo:** transformar cada módulo em tarefa Celery, com as três filas (`gpu`, `cpu`, `io`).

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seções 1, B1). Configure src/core/celery_app.py:
- Broker e backend = Redis.
- task_acks_late=True, task_reject_on_worker_lost=True.
- Três filas: 'gpu' (concorrência 1), 'cpu', 'io'.
- soft_time_limit/time_limit padrão por tipo de tarefa.

Em src/tasks/, crie wrappers Celery para cada etapa do pipeline (A1-A6), cada um roteado
para a fila certa (gpu: script/tts/visual/caption; cpu: assembly; io: upload).
Cada task: idempotente, atualiza a tabela `jobs` (status, error_msg, retries), com retry
exponencial. Crie uma função build_video_chain(topic_id) que encadeia as tasks na ordem
correta usando chain(), com um ponto de parada para revisão humana antes do upload.
Escreva testes com celery em modo eager (task_always_eager=True).
```

**Checklist de checkpoint B1:**
- [ ] As 3 filas existem e cada task vai para a fila certa.
- [ ] A fila `gpu` processa uma task por vez (concorrência 1 confirmada).
- [ ] Cada task atualiza a tabela `jobs` corretamente (queued→running→done/failed).
- [ ] Falha de task gera retry e registra `error_msg`.
- [ ] `build_video_chain` produz um vídeo completo, parando para revisão antes do upload.
- [ ] Testes passam em modo eager.

## B2 — Agendamento (Celery Beat)

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção B2). Configure Celery Beat (singleton) para:
- Distribuir publicações ao longo do dia (não em rajada) nos horários de pico por canal.
- Disparar coleta de métricas diária (Bloco E1).
- Disparar varredura de tendências (D1) e concorrentes (D2) em intervalos configuráveis.
- Disparar o health monitor (F1).
Tudo configurável via DB/config, não hardcoded. Garanta um único processo Beat.
```

**Checklist de checkpoint B2:**
- [ ] Publicações são distribuídas, não despejadas de uma vez.
- [ ] Jobs agendados (métricas, trends, health) disparam nos intervalos certos.
- [ ] Apenas um processo Beat roda (sem tarefas duplicadas).

---

# BLOCO C — Dashboard

## C1 — Backend FastAPI (`dashboard/main.py`, `dashboard/routers/`)

**Objetivo:** API que lê o estado do sistema e expõe controles.

**Endpoints mínimos:** `/channels`, `/videos`, `/jobs`, `/metrics`, `/topics`, `/alerts`, `/pipeline/control`.

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seções 4, C1). Implemente o app FastAPI em src/dashboard/main.py
com routers separados por recurso. Use SQLAlchemy async. Endpoints REST para listar/filtrar
channels, videos, jobs, metrics, topics, alerts. Endpoints de controle em /pipeline/control:
- POST /pipeline/run {channel_id, topic_id} -> dispara build_video_chain
- POST /pipeline/pause {channel_id}, /resume {channel_id}
- POST /videos/{id}/reprocess {from_stage} -> re-enfileira do estágio que falhou
- POST /videos/{id}/approve -> libera para upload (revisão humana)
Validação com pydantic. Trate erros com HTTPException. Escreva testes com httpx AsyncClient.
```

**Checklist de checkpoint C1:**
- [ ] `GET` de cada recurso retorna dados reais do banco, com filtros.
- [ ] `POST /pipeline/run` dispara uma chain de verdade.
- [ ] `reprocess` re-enfileira a partir do estágio certo (não do zero).
- [ ] `approve` é o gate humano antes do upload.
- [ ] Docs automáticas em `/docs` funcionam.
- [ ] Testes de API passam.

## C2 — Frontend (`dashboard/templates/` ou app React)

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção C2). Construa um frontend de dashboard (HTMX + Jinja2).
Páginas: (1) Visão geral — cards de receita total, vídeos publicados, alertas ativos;
(2) Canais — tabela com status, RPM, saúde; (3) Vídeos — fila por status, com botões
approve/reprocess; (4) Jobs — em tempo real (em fila/rodando/falho); (5) Tendências —
pautas sugeridas com score, botões aprovar/rejeitar; (6) Receita — gráficos no tempo.
Consuma a API do C1. Mantenha simples e funcional. Sem framework pesado.
```

**Checklist de checkpoint C2:**
- [ ] Cada página carrega dados reais da API.
- [ ] Botões approve/reprocess/aprovar-pauta funcionam de ponta a ponta.
- [ ] Painel de jobs atualiza sem reload manual (HTMX polling).
- [ ] Gráficos de receita renderizam.

## C3 — Controle de pipeline (transversal)
Já coberto por C1 (`/pipeline/control`) + B1 (flags via Redis). **Checkpoint:** start/stop, reprocess e approve funcionam pelo dashboard, refletindo no Celery/DB.

---

# BLOCO D — Inteligência de conteúdo

## D1 — Scraper de tendências (`intelligence/trends.py`)

**Objetivo:** descobrir pautas com potencial no nicho. Resolve "sobre o que fazer o próximo vídeo".

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seções 4, D1). Implemente src/intelligence/trends.py:
Colete pautas de: Google Trends (pytrends), YouTube (Data API — search por nicho,
ordenado por viewCount recente) e Reddit (PRAW — rising/hot nos subreddits do nicho).
Normalize tudo em registros `topics` com um potential_score calculado (combine: volume
de busca, recência, engajamento). Deduplique. Salve com status='new'. Roda na fila 'io'.
Escreva testes mockando cada fonte.
```

**Checklist de checkpoint D1:**
- [ ] Coleta pautas das 3 fontes e grava em `topics`.
- [ ] `potential_score` é calculado e ordena pautas de forma sensata.
- [ ] Deduplicação funciona (não repete a mesma pauta).
- [ ] Rate limits das APIs tratados.

## D2 — Espião de concorrentes (`intelligence/competitors.py`)

**Objetivo:** detectar vídeos que estouraram em canais do nicho, para você fazer sua versão (com ângulo próprio — é research, não cópia).

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção D2). Implemente src/intelligence/competitors.py:
Para uma lista de canais concorrentes (config/DB), use a YouTube Data API para pegar os
uploads recentes e suas views. Calcule views/hora desde a publicação e sinalize vídeos
cujo desempenho está N desvios-padrão acima da média daquele canal ("outlier"/breakout).
Gere registros `topics` com source='competitor' apontando o tema (NÃO o vídeo a copiar).
Roda na fila 'io'. Testes mockando a API.
```

**Checklist de checkpoint D2:**
- [ ] Identifica corretamente vídeos "outlier" (alto views/hora vs. média do canal).
- [ ] Gera pautas com `source='competitor'` focadas no TEMA, não em copiar.
- [ ] Não duplica pautas já existentes.

## D3 — Detector de similaridade anti-inautenticidade (`intelligence/similarity.py`)

**Objetivo:** medir quão parecidos são seus vídeos recentes e **alertar antes** de cair no padrão "template replicável" que o YouTube pune. (Esta feature sozinha justifica o dashboard.)

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seções 4, D3, 10). Implemente src/intelligence/similarity.py:
Use sentence-transformers (modelo multilíngue, roda local na GPU) para gerar embeddings
do roteiro de cada vídeo. Ao processar um novo vídeo, calcule a similaridade de cosseno
contra os últimos N vídeos do MESMO canal. Grave em videos.similarity_score.
Se a similaridade média passar de um limiar configurável (ex.: 0.85), gere um `alert`
do tipo 'warning' ANTES do upload, bloqueando a publicação automática até revisão humana.
Escreva testes com roteiros propositalmente parecidos e diferentes.
```

**Checklist de checkpoint D3:**
- [ ] Calcula `similarity_score` para cada vídeo novo.
- [ ] Roteiros muito parecidos geram score alto; diferentes, score baixo.
- [ ] Ultrapassar o limiar gera `alert` e **bloqueia** publicação automática.
- [ ] Roda local (sem custo de API).

---

# BLOCO E — Receita

## E1 — Agregador de receita (`revenue/aggregator.py`)

**Objetivo:** consolidar receita de todas as fontes por canal num só lugar.

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seções 4, E1). Implemente src/revenue/aggregator.py:
Colete diariamente (via Celery Beat) métricas e receita por vídeo usando a YouTube
Analytics API v2 (reports.query): views, estimatedMinutesWatched, estimatedRevenue, cpm,
monetizedPlaybacks, averageViewDuration. Scope de receita: yt-analytics-monetary.readonly.
Se a receita por canal não vier confiável via Analytics API, prepare suporte à YouTube
Reporting API (relatórios bulk CSV). Some também receita de afiliados (Bloco H2) por canal.
Grave em `metrics`. Trate o delay de finalização dos dados (~48h). Testes mockando a API.
```

**Checklist de checkpoint E1:**
- [ ] Coleta métricas reais por vídeo e grava em `metrics`.
- [ ] Receita estimada aparece por canal no dashboard.
- [ ] Lida com o atraso de 48h dos dados (não trata dado parcial como final).
- [ ] Soma receita de afiliados quando disponível.

## E2 — Lucro real por vídeo (`revenue/profit.py`)

**Objetivo:** cruzar custo (GPU + energia) com receita. Revela canais/formatos que dão prejuízo escondido — quase ninguém mede isso.

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seções 4, E2). Implemente src/revenue/profit.py:
Instrumente o pipeline para registrar gpu_seconds por vídeo (tempo gasto nas etapas GPU).
Estime kwh a partir de uma potência média configurável da GPU/sistema e do tempo.
Multiplique pela tarifa de energia (config, R$/kWh) para obter computed_cost. Some custos
de API se houver. Grave em `costs`. Exponha "lucro líquido por vídeo" e "por canal"
(receita de `metrics` menos custo de `costs`). Testes com valores conhecidos.
```

**Checklist de checkpoint E2:**
- [ ] `gpu_seconds` é registrado por vídeo de forma real.
- [ ] `computed_cost` em R$ bate com um cálculo manual de conferência.
- [ ] Dashboard mostra lucro líquido por vídeo e por canal.
- [ ] Dá pra identificar um formato/canal deficitário.

## E3 — A/B test de thumbnail/título (`revenue/ab_testing.py`)

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seções 4, E3). Implemente src/revenue/ab_testing.py:
Gere 2-3 variantes de thumbnail (e/ou título) por vídeo. Registre em `ab_variants`.
Use a YouTube Analytics API para puxar impressões e CTR por variante (quando disponível
via teste nativo) OU implemente rotação manual de thumbnail por janela de tempo,
medindo CTR de cada período. Marque is_winner pela maior CTR estatisticamente relevante.
Testes mockando as métricas.
```

**Checklist de checkpoint E3:**
- [ ] Registra variantes e coleta CTR de cada uma.
- [ ] Escolhe vencedora por CTR (com mínimo de impressões).
- [ ] Resultado visível no dashboard.

---

# BLOCO F — Operações

## F1 — Health monitor (`operations/health.py`)

**Objetivo:** alertar sobre strike, queda de RPM, desmonetização, jobs falhos. Dado o risco de plataforma, isso é seguro de vida.

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seções 4, F1). Implemente src/operations/health.py:
Rode periodicamente (Celery Beat). Cheque: (a) quedas abruptas de RPM/receita vs. média
móvel; (b) vídeos que perderam monetização; (c) jobs em status 'failed'; (d) sinais de
strike (via API quando disponível). Gere registros `alerts` com severidade. Envie alertas
críticos para um webhook (Discord/Telegram, URL via config) e push pro dashboard.
Testes simulando cada condição de alerta.
```

**Checklist de checkpoint F1:**
- [ ] Queda de RPM/receita acima do limiar gera alerta.
- [ ] Job falho e desmonetização geram alerta.
- [ ] Alertas críticos chegam ao Discord/Telegram.
- [ ] Alertas aparecem no dashboard e podem ser "acknowledged".

## F2 — Repurpose long→shorts (`operations/repurpose.py`)

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção F2). Implemente src/operations/repurpose.py:
A partir de um vídeo long-form (transcrição + vídeo), identifique os 3-5 melhores trechos
(picos de densidade/retenção via análise da transcrição com o LLM local) e corte-os em
Shorts 9:16 via FFmpeg, com legendas. Cada Short vira um novo registro `videos`.
Roda: análise na fila 'gpu', cortes na fila 'cpu'. Testes com um vídeo de exemplo.
```

**Checklist de checkpoint F2:**
- [ ] Gera 3-5 Shorts coerentes a partir de 1 long-form.
- [ ] Cada Short é 9:16, com legendas, e vira registro `videos`.
- [ ] Trechos escolhidos fazem sentido (não cortes aleatórios).

## F3 — SEO generator (`operations/seo.py`)

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção F3). Implemente src/operations/seo.py:
A partir do roteiro, gere via LLM local: título otimizado (com variantes para A/B),
descrição, tags e capítulos (timestamps). Roda na fila 'gpu'. Testes mockando o LLM.
```

**Checklist de checkpoint F3:**
- [ ] Gera título, descrição, tags e capítulos a partir do roteiro.
- [ ] Saídas plugam direto no upload (A6) e no A/B (E3).

---

# BLOCO G — Engajamento

## G1 — Triagem de comentários (`engagement/comments.py`)

**Objetivo:** manter o sinal humano de responder comentários (que o algoritmo valoriza) sem você ler 500 — com **você aprovando** antes de postar.

**Prompt para o Cursor:**
```
Leia @ARQUITETURA.md (Seção G1). Implemente src/engagement/comments.py:
Puxe comentários novos via YouTube Data API. Classifique por sentimento e prioridade
(perguntas reais > elogios > spam). Para os prioritários, gere um rascunho de resposta
via LLM local. NÃO poste automaticamente: grave os rascunhos para revisão e aprovação no
dashboard. Só após approve humano, poste via API. Testes mockando a API e o LLM.
```

**Checklist de checkpoint G1:**
- [ ] Busca e classifica comentários por prioridade/sentimento.
- [ ] Gera rascunhos só para os prioritários.
- [ ] **Nada é postado sem aprovação humana** no dashboard.

---

# BLOCO H — Extras (com ressalvas legais/operacionais)

> Leia a Seção 10 antes de implementar. Duas destas funções têm risco real.

## H1 — Bot de X/Twitter (`extras/x_bot.py`)
**Ressalva:** a API do X é paga (tier gratuito quase inútil para posting; pagos a partir de ~US$100/mês) e bane automação agressiva.
**Prompt resumido:** implemente posting agendado via API oficial do X (tweepy), com limites conservadores e backoff. Repurpose de Shorts/insights como posts. Sem comportamento de spam.
**Checkpoint:** posta dentro dos limites da API, sem bursts; trata erros de rate/cota.

## H2 — Afiliados (`extras/affiliate.py`)
**Ressalva:** baixo risco. Siga as regras de disclosure de cada programa.
**Prompt resumido:** integre Amazon Associates + programas BR (Hotmart, Mercado Livre, Shopee). Gere/insira links de afiliado nas descrições, rastreie cliques/conversões por vídeo e some a receita no E1.
**Checkpoint:** links corretos por vídeo; receita de afiliado aparece no agregador (E1).

## H3 — Cold outreach (`extras/outreach.py`) — ATENÇÃO LGPD
**Ressalva séria:** no Brasil, e-mail de pessoa física é dado pessoal sob a **LGPD**. Disparo em massa a partir de base raspada = risco de multa, blacklist e domínio queimado. Só faça **B2B, segmentado, ultra-personalizado, com opt-out claro e baixo volume.** Não é spam em massa.
**Prompt resumido:** implemente um módulo de outreach B2B que monta uma lista pequena e qualificada de empresas, gera mensagens personalizadas (não-template) via LLM, com opt-out obrigatório, limite diário baixo, e logging de consentimento/contato. Use um domínio/sender separado do principal.
**Checkpoint:** volume baixo; cada mensagem é personalizada; opt-out presente; nada dispara sem sua aprovação; domínio de envio isolado.

---

## 9. Roadmap por fases (com portões de checkpoint)

Cada fase só "fecha" quando **todos** os checkpoints dos módulos dela passam. Não pule fases.

### Fase 0 — Prova de conceito (1 vídeo, manual) — semanas 1–2
**Módulos:** A1, A2, A3, A4, A5, A6 (+ infra mínima: config, database, models).
**Portão da fase:** você consegue, rodando um comando, gerar **um vídeo completo** a partir de uma pauta digitada à mão, revisar, e subir como `private` no YouTube. Sem Celery ainda — pode ser um script sequencial.

### Fase 1 — Pipeline orquestrado (1 canal) — semanas 3–5
**Módulos:** B1, B2, C1, C2 (dashboard básico), F3 (SEO).
**Portão da fase:** pelo dashboard você dispara uma run, acompanha os jobs, aprova o vídeo e ele sobe. Métricas básicas visíveis. Publicações agendadas, não em rajada.

### Fase 2 — Inteligência + receita (1 canal, otimizado) — semanas 6–9
**Módulos:** D1, D2, D3, E1, E2, F1.
**Portão da fase:** o dashboard sugere pautas com score, te alerta de similaridade alta (D3) e de problemas de saúde (F1), e mostra **lucro líquido por vídeo** (E2). Aqui o sistema deixa de ser "fábrica cega" e vira "fábrica com inteligência".

### Fase 3 — Escala e extras (multi-canal) — semanas 10+
**Módulos:** E3, F2, G1, H1/H2 (H3 só se realmente necessário), multi-canal.
**Portão da fase:** dois ou mais canais **genuinamente diferenciados** rodando, com A/B de thumbnail, repurpose long→shorts, triagem de comentários com aprovação humana, e afiliados somando na receita. Só lance o canal 2 **depois** que o canal 1 estiver monetizado.

---

## 10. Restrições de plataforma que o código DEVE respeitar (resumo)

Estas regras estão embutidas nos checkpoints acima, mas valem como princípio transversal — o Cursor não conhece o contexto de risco, então **você** garante:

1. **Revisão humana obrigatória antes do upload** (gate `approve`). O sistema nunca publica sozinho sem um humano no comando do tópico/ângulo/thumbnail.
2. **Detector de similaridade (D3) bloqueia** publicação automática quando vídeos ficam parecidos demais (padrão "template replicável" = alvo da política de conteúdo inautêntico do YouTube).
3. **Variação real entre canais.** Multi-canal não é o mesmo esqueleto clonado N vezes — voz, estrutura e identidade diferentes por canal (senão, "cascade demonetization").
4. **Rótulo de IA** aplicado quando exigido (voz/eventos sintéticos realistas); narração com sua própria voz/voz licenciada não precisa.
5. **Licenças respeitadas:** Kokoro/Fish Speech (TTS), SDXL/FLUX Schnell (imagem). Nunca XTTS/F5-TTS/FLUX Dev para conteúdo monetizado.
6. **TikTok:** sem autopostar via API não-auditada nem automação de browser (viola ToS).
7. **Diversifique a renda:** o AdSense é uma fonte, não a única. O código já prevê afiliados e métricas consolidadas justamente por isso.
8. **LGPD no outreach (H3):** B2B, segmentado, opt-out, baixo volume, sender isolado.

---

## Anexo — Ressalvas honestas

- **Risco de plataforma é existencial.** Canais foram **terminados** (não só desmonetizados) em 2026. Nunca dependa só do YouTube; a arquitetura prioriza monitoramento (F1) e diversificação (E1/H2) por isso.
- **"Fábrica 100% automática" não existe de forma sustentável.** O modelo viável é híbrido: IA no trabalho braçal, humano no comando editorial. Os gates de aprovação no código são intencionais — não os remova para "ganhar velocidade".
- **O Cursor vai errar** em integração entre módulos, tratamento de erro de APIs externas e licenças. Sua revisão é parte da arquitetura, não um extra.
- **Números de RPM/CPM são estimativas.** Use seu próprio YouTube Studio como fonte primária.
- **APIs e políticas mudam rápido** (TikTok, YouTube, X). Verifique a documentação oficial antes de decisões importantes; trate as chaves/escopos como configuráveis.
