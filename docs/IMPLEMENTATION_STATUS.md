# Estado real da implementação

**Checkpoint:** 2026-07-23

**Escopo:** Fase 1 e primeira fatia da Fase 2

**Branch:** `chore/project-skills`

## Veredito

O repositório possui uma base parcial e testável do Content Engine, mas ainda não possui o
runtime completo da plataforma. A geração de roteiro A1, a configuração, a sessão de banco e os
modelos SQLAlchemy orientados a conteúdo têm código real. O kernel compartilhado persiste
`Automation`, `Run`, `StepRun`, `Approval`, controles de execução e cada transição de estado, e
executa um único exemplo no-op em processo local. Celery, control plane e as demais etapas de
automação são placeholders ou itens planejados.

## Classificação por área

| Área | Estado | Evidência e limite atual |
| --- | --- | --- |
| Identidade técnica | Implementado | `pyproject.toml`, `.env.example`, `src/core/config.py` e `docker-compose.yml` usam `automation-foundry` nos identificadores ativos. “Content Engine” permanece como nome legítimo do módulo. |
| Configuração | Parcial | `src/core/config.py` implementa settings tipados, leitura de `.env` e defaults locais. Campos de credenciais existem, mas criptografia, validação operacional e adapters consumidores ainda não existem. |
| Banco e sessão | Parcial | `src/core/database.py` cria engine e sessão async com commit/rollback. O default de processo único é SQLite; o caminho PostgreSQL concorrente ainda não foi implementado nem validado. |
| Modelos de domínio | Parcial | `src/core/models.py` implementa `Channel`, `Video`, `Job`, `Metric`, `Cost`, `Topic`, `Alert` e `ABVariant`. O schema é específico do Content Engine e não contém os contratos genéricos da Fase 2. |
| Kernel compartilhado | Parcial | `src/platform/models.py` implementa `Automation`, `Run`, `StepRun`, `Approval`, controles persistidos e históricos append-only. Approval liga run, ação e digest do payload, tem decisão imutável e só autoriza uma chave idempotente de task. Proteção de corrida entre workers concorrentes ainda não existe. |
| Migrations | Parcial | `alembic.ini`, `alembic/env.py` e quatro revisions incrementais formam um caminho verificável para as oito tabelas compartilhadas. As tabelas legadas do Content Engine permanecem deliberadamente fora dessa baseline. |
| Content Engine — A1 | Implementado | `src/pipeline/script_gen.py` contém geração em quatro chamadas, retry limitado, saneamento de saída e persistência local de roteiro/narração. `tests/pipeline/test_script_gen.py` cobre o comportamento com Ollama mockado. |
| Content Engine — mídia e publicação | Placeholder | `src/pipeline/tts.py`, `visuals.py`, `captions.py`, `assembly.py` e `upload.py` estão vazios. Nenhuma publicação real foi implementada. |
| Orquestração e filas | Parcial | `src/platform/task_runner.py` executa corrotinas em processo único com timeout, retries limitados, backoff exponencial e erros redigidos. Controles persistidos são consultados antes e depois da corrotina e entre tentativas; o wrapper não preempta código síncrono bloqueante. `src/core/celery_app.py` e `src/tasks/` continuam vazios. |
| CLI operacional | Parcial | `src/cli.py` expõe diagnóstico, exemplo, controles e os comandos `request-approval`/`decide-approval`. Decisões exigem actor e motivo, mas a CLI local ainda não autentica criptograficamente o actor. Não há comandos de retry ou operação de workers. |
| Control plane | Placeholder | `src/dashboard/main.py`, `src/dashboard/routers/__init__.py` e `src/dashboard/templates/.gitkeep` estão vazios. A CLI de diagnóstico não inicia nem antecipa a aplicação FastAPI da Fase 3. |
| Inteligência | Placeholder | `src/intelligence/trends.py`, `competitors.py` e `similarity.py` estão vazios. |
| Engajamento | Placeholder | `src/engagement/comments.py` está vazio. |
| Operações | Placeholder | `src/operations/health.py`, `seo.py` e `repurpose.py` estão vazios. |
| Receita e experimentos | Placeholder | `src/revenue/aggregator.py`, `profit.py` e `ab_testing.py` estão vazios; só os modelos de dados relacionados existem. |
| Extras e ações externas | Placeholder | `src/extras/affiliate.py`, `outreach.py` e `x_bot.py` estão vazios. Não há envio, gasto ou ação externa implementada. |
| Trading Research Lab | Planejado | O domínio aparece em `ARQUITETURA.md` e `ROADMAP.md`, mas não possui implementação. Trading com dinheiro real permanece fora do escopo. |
| Infraestrutura local | Parcial | `docker-compose.yml` define somente Redis com AOF. PostgreSQL, workers e health checks ainda não estão definidos. Ollama é esperado no host, fora do Compose. |
| Testes | Parcial | Há 107 testes para modelos legados, A1, CLI, lifecycles, task wrapper, controles, approval gates, integração SQLite e smoke/roundtrip das migrations. Não há testes de Celery, dashboard, PostgreSQL, integrações reais, restart ou hardware. |

Arquivos `__init__.py` vazios são marcadores de pacote e não contam como funcionalidade.

## Comandos canônicos da Fase 1

Em um checkout limpo no Windows, sem instalar os extras pesados `ai`:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy src
.venv\Scripts\automation-foundry doctor
docker compose config
```

O schema compartilhado da Fase 2 é atualizado explicitamente com:

```powershell
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m alembic check
```

Esses comandos gerenciam somente `automations`, `runs`, `run_transitions`, `step_runs` e
`step_run_transitions`; não criam nem alteram as tabelas legadas do Content Engine.

Depois da migration, a execução manual observável e sem efeito externo usa:

```powershell
.venv\Scripts\automation-foundry run-example --idempotency-key smoke-001
```

O comando persiste uma automação fixa, uma run e um step `io` no-op. O wrapper confirma o estado
`running` antes da operação e confirma o resultado antes de encerrar a run, permitindo observar
e recuperar tentativas interrompidas. Repetir a mesma chave retorna o resultado concluído sem
criar novas transições. Este caminho é
deliberadamente de processo único e não valida concorrência, Redis, Celery ou GPU.

Controles locais explícitos usam um motivo obrigatório:

```powershell
.venv\Scripts\automation-foundry kill-switch --automation-slug platform-smoke --enable --reason "maintenance"
.venv\Scripts\automation-foundry kill-switch --automation-slug platform-smoke --disable --reason "review completed"
.venv\Scripts\automation-foundry cancel-run --run-id 1 --reason "operator request"
```

Um gate para o payload atual da run usa:

```powershell
.venv\Scripts\automation-foundry request-approval --run-id 1 --idempotency-key "publish:1" --action publish --summary "Review private upload"
.venv\Scripts\automation-foundry decide-approval --approval-id 1 --approve --actor "local-owner" --reason "review passed"
```

Rejeitar usa `--reject`. O actor é registrado para auditoria local, mas não representa autenticação
forte nesta fase.

`automation-foundry doctor` é a execução canônica e sem efeitos colaterais da Fase 1. Use
`doctor --json` para saída estruturada e `doctor --services` somente quando Redis e Ollama locais
devem estar ativos. O Redis pode ser iniciado com `docker compose up -d redis`. A CLI não inicia
o dashboard: `src/dashboard/main.py` continua como placeholder até a Fase 3.

## Baseline observado neste checkpoint

O ambiente fornecido possui Python 3.12.13. A primeira coleta, antes da instalação das
dependências do projeto, produziu:

- `python -m pytest -q`: interrompido na coleta por ausência de `sqlalchemy` e `httpx`;
- `python -m ruff check .`: não executado porque o módulo `ruff` não estava instalado;
- `python -m mypy src`: não executado porque o módulo `mypy` não estava instalado.

Após criar `.venv` e instalar `.[dev]` sem os extras `ai`:

- `python -m pip install -e ".[dev]"`: concluído; a primeira tentativa foi bloqueada pela rede da
  sandbox e a repetição com acesso autorizado concluiu a instalação;
- `python -m pytest -q`: **107 passed**;
- `python -m ruff check .`: encontrou 7 ocorrências mecânicas preexistentes; o autofix removeu
  imports não usados, ordenou imports e simplificou um context manager; a repetição terminou com
  **All checks passed**;
- `python -m mypy src`: **Success: no issues found in 39 source files**;
- validação estrutural de `pyproject.toml` e `docker-compose.yml` por `tomllib` e YAML:
  concluída, incluindo metadata, entry point `src.cli:main` e nome do container;
- `automation-foundry doctor`: concluído sem serviços externos, com checks estáticos aprovados e
  sondas de Redis/Ollama explicitamente marcadas como `skip`;
- `automation-foundry doctor --json`: concluído com código 0 e payload redigido;
- `automation-foundry doctor --services`: código 1 esperado neste ambiente de viagem, com Redis e
  Ollama reportados como indisponíveis em loopback;
- `docker compose config`: bloqueado porque o executável Docker não está disponível neste
  ambiente.
- migrations Alembic em SQLite temporário: `upgrade head` e `alembic check` concluídos; somente as
  oito tabelas compartilhadas e `alembic_version` foram criadas.

## Verificações bloqueadas ou adiadas

- **Computador de casa:** confirmar Windows, CPU, RAM, disco e processo de startup/shutdown.
- **GPU:** confirmar RTX 3090, VRAM, CUDA, modelos, consumo e fila GPU com concorrência 1.
- **Serviços locais:** validar Ollama, Redis, PostgreSQL e recuperação após indisponibilidade.
- **Credenciais:** validar somente em modo opt-in as APIs oficiais de Google/YouTube, Reddit,
  bancos de mídia, X e demais provedores. Nenhuma credencial deve entrar no Git.
- **Ações externas:** publicação, mensagens, gastos, exclusões materiais e ações financeiras
  continuam sem implementação. O kill switch genérico existe; approval e gates específicos de
  domínio ainda são obrigatórios antes de qualquer teste externo.

## Próxima fatia recomendada

Adicionar `Artifact` persistente com caminho local, checksum, tamanho, tipo, sensibilidade e
retenção, ligando resultados de steps a evidências verificáveis. Concorrência, Redis, Celery e
hard time limits continuam dependentes do computador de casa.
