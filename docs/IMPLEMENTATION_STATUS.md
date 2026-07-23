# Estado real da implementação

**Checkpoint:** 2026-07-23

**Escopo:** Fase 1 e fatias locais verificáveis das Fases 2 e 3

**Branch:** `chore/project-skills`

## Veredito

O repositório possui uma base parcial e testável do Content Engine, mas ainda não possui o
runtime completo da plataforma. A geração de roteiro A1, a configuração, a sessão de banco e os
modelos SQLAlchemy orientados a conteúdo têm código real. O kernel compartilhado persiste
`Automation`, `Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, `MetricPoint`, `LedgerEntry`, `Alert`, controles de execução e cada transição de estado, e
executa um único exemplo no-op em processo local. Celery, control plane e as demais etapas de
automação são placeholders ou itens planejados.

## Classificação por área

| Área | Estado | Evidência e limite atual |
| --- | --- | --- |
| Identidade técnica | Implementado | `pyproject.toml`, `.env.example`, `src/core/config.py` e `docker-compose.yml` usam `automation-foundry` nos identificadores ativos. “Content Engine” permanece como nome legítimo do módulo. |
| Configuração | Parcial | `src/core/config.py` implementa settings tipados, leitura de `.env` e defaults locais. Campos de credenciais existem, mas criptografia, validação operacional e adapters consumidores ainda não existem. |
| Banco e sessão | Parcial | `src/core/database.py` cria engine e sessão async com commit/rollback. O default de processo único é SQLite; o caminho PostgreSQL concorrente ainda não foi implementado nem validado. |
| Modelos de domínio | Parcial | `src/core/models.py` implementa `Channel`, `Video`, `Job`, `Metric`, `Cost`, `Topic`, `Alert` e `ABVariant`. O schema é específico do Content Engine e não contém os contratos genéricos da Fase 2. |
| Kernel compartilhado | Parcial | `src/platform/models.py` implementa `Automation`, `Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, `MetricPoint`, `LedgerEntry`, `Alert`, controles persistidos e históricos append-only. Approval liga run, ação e digest do payload, tem decisão imutável e só autoriza uma chave idempotente de task. Artifact registra metadados verificados de arquivos confinados ao storage local. Schedule nasce desabilitado, valida cron/timezone, calcula a próxima ocorrência e audita mudanças; ainda não dispara runs. MetricPoint registra observações decimais imutáveis, idempotentes e atribuíveis a automation/run/step, sem agregação automática. LedgerEntry registra custo, receita ou valor atribuível como observação decimal imutável e idempotente, com escopo, moeda, categoria, fonte, confiança opcional e timestamp explícitos; não paga, cobra nem transfere. Alert deduplica ocorrências, escala severidade, audita reconhecimento/resolução e reabre somente com ocorrência posterior; ainda não envia notificações. Proteção de corrida entre workers concorrentes ainda não existe. |
| Migrations | Parcial | `alembic.ini`, `alembic/env.py` e nove revisions incrementais formam um caminho verificável para as quinze tabelas compartilhadas. `platform_alerts` evita colisão com `alerts` do Content Engine legado. As tabelas legadas permanecem deliberadamente fora dessa baseline. |
| Content Engine — A1 | Implementado | `src/pipeline/script_gen.py` contém geração em quatro chamadas, retry limitado, saneamento de saída e persistência local de roteiro/narração. `tests/pipeline/test_script_gen.py` cobre o comportamento com Ollama mockado. |
| Content Engine — mídia e publicação | Placeholder | `src/pipeline/tts.py`, `visuals.py`, `captions.py`, `assembly.py` e `upload.py` estão vazios. Nenhuma publicação real foi implementada. |
| Orquestração e filas | Parcial | `src/platform/task_runner.py` executa corrotinas em processo único com timeout, retries limitados, backoff exponencial e erros redigidos. Controles persistidos são consultados antes e depois da corrotina e entre tentativas; o wrapper não preempta código síncrono bloqueante. `src/core/celery_app.py` e `src/tasks/` continuam vazios. |
| CLI operacional | Parcial | `src/cli.py` expõe diagnóstico, dashboard operacional, exemplo, controles, approvals, artifacts, schedules, `record-metric`, `record-ledger-entry`, `record-alert` e `set-alert`. O comando `dashboard` usa apenas host validado como loopback. Criação de schedule é sempre desabilitada; habilitar ou desabilitar exige actor e motivo. Métricas e valores financeiros entram como strings decimais. A saída do ledger omite categoria, fonte e chave idempotente; alertas não repetem o summary. A CLI local ainda não autentica criptograficamente o actor e não há comandos de retry ou operação de workers. |
| Control plane | Parcial | `src/dashboard/main.py`, `service.py` e os templates implementam visão geral read-only e detalhe da run com evidências vinculadas. Cancelamento de run e rejeição de approval usam POST com token CSRF por processo, Host loopback, confirmação e motivo; a rejeição também exige actor. Os serviços do kernel preservam idempotência, decisão imutável e eventos auditáveis. Motivos, actors, payloads e summaries não voltam ao HTML. Aprovação positiva permanece deliberadamente ausente porque o payload protegido ainda não possui projeção de revisão segura; também não há start, retry, kill switch, health ou HTMX interativo. A identidade do actor local ainda não é autenticada. |
| Inteligência | Placeholder | `src/intelligence/trends.py`, `competitors.py` e `similarity.py` estão vazios. |
| Engajamento | Placeholder | `src/engagement/comments.py` está vazio. |
| Operações | Placeholder | `src/operations/health.py`, `seo.py` e `repurpose.py` estão vazios. |
| Receita e experimentos | Placeholder | `src/revenue/aggregator.py`, `profit.py` e `ab_testing.py` estão vazios; só os modelos de dados relacionados existem. |
| Extras e ações externas | Placeholder | `src/extras/affiliate.py`, `outreach.py` e `x_bot.py` estão vazios. Não há envio, gasto ou ação externa implementada. |
| Trading Research Lab | Planejado | O domínio aparece em `ARQUITETURA.md` e `ROADMAP.md`, mas não possui implementação. Trading com dinheiro real permanece fora do escopo. |
| Infraestrutura local | Parcial | `docker-compose.yml` define somente Redis com AOF. PostgreSQL, workers e health checks ainda não estão definidos. Ollama é esperado no host, fora do Compose. |
| Testes | Parcial | Há 196 testes para modelos legados, A1, CLI, lifecycles, task wrapper, controles, approval gates, artifacts, schedules com timezone/DST, métricas, ledger financeiro observacional, alertas deduplicados/auditados, dashboard, cancelamento e rejeição de approval protegidos/idempotentes, integração SQLite e smoke/roundtrip das migrations. Não há testes de Celery, PostgreSQL, integrações reais, restart ou hardware. |

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

Esses comandos gerenciam somente as quinze tabelas compartilhadas: `automations`, `runs`,
`run_transitions`, `step_runs`, `step_run_transitions`, `control_events`, `approvals`,
`approval_events`, `artifacts`, `schedules`, `schedule_events`, `metric_points`, `ledger_entries`, `platform_alerts`
e `platform_alert_events`; não criam nem alteram as tabelas
legadas do Content Engine.

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

Depois que um worker ou operador criar um arquivo abaixo de `STORAGE_ROOT` (default `./storage`),
seus metadados podem ser registrados sem copiar ou alterar o conteúdo:

```powershell
.venv\Scripts\automation-foundry register-artifact --run-id 1 --idempotency-key "run:1:script" --artifact-type script --path "content-engine/1/script/script.json" --media-type application/json --origin content-engine --sensitivity internal --retention-days 30
```

O caminho pode ser absoluto ou relativo ao storage, mas precisa resolver dentro da raiz configurada.
O comando calcula tamanho e SHA-256 do arquivo real, persiste somente o caminho relativo e aceita
`--expected-sha256` quando o produtor já conhece o digest. A política de retenção é apenas
metadado: esta fatia não apaga arquivos.

Uma agenda recorrente é criada desabilitada e só recebe `next_run_at` após ação explícita:

```powershell
.venv\Scripts\automation-foundry create-schedule --automation-slug platform-smoke --name daily-check --cron "0 9 * * *" --timezone America/Sao_Paulo --actor "local-owner" --reason "reviewed schedule"
.venv\Scripts\automation-foundry set-schedule --schedule-id 1 --enable --actor "local-owner" --reason "start recurring work"
.venv\Scripts\automation-foundry set-schedule --schedule-id 1 --disable --actor "local-owner" --reason "pause recurring work"
```

Cron usa exatamente cinco campos POSIX e timezone IANA. As datas são calculadas com timezone e
persistidas em UTC. `allow_overlap` e `misfire_grace_seconds` já fazem parte do contrato, mas não
são aplicados porque esta fatia não possui dispatcher nem Celery Beat ativo.

Uma observação numérica local pode ser atribuída somente à automação ou também a uma run/step:

```powershell
.venv\Scripts\automation-foundry record-metric --automation-slug platform-smoke --run-id 1 --step-run-id 1 --idempotency-key "run:1:step:1:duration" --name step.duration --kind duration --value 12.345 --unit s --source task-runner --confidence 0.99
```

Os tipos aceitos são `counter`, `gauge`, `duration`, `ratio` e `currency`. O valor é validado e
persistido como decimal com até dez casas; SQLite usa texto canônico para não arredondar e o
schema PostgreSQL usa `NUMERIC(30,10)`. Ratios usam intervalo 0–1 e unidade `ratio`, e moedas
usam código de três letras maiúsculas. Repetir a chave com o mesmo conteúdo retorna o ponto
existente; conteúdo divergente é rejeitado. Esta fatia não agrega, coleta nem envia métricas.

Uma observação financeira local pode registrar custo, receita ou valor atribuível com escopo
consistente e, opcionalmente, ligar a evidência a um metric point do mesmo escopo:

```powershell
.venv\Scripts\automation-foundry record-ledger-entry --automation-slug platform-smoke --run-id 1 --step-run-id 1 --metric-point-id 1 --idempotency-key "run:1:step:1:value" --type attributed_value --category human-time-saved --amount 125.50 --currency BRL --source local-estimate --confidence 0.75
```

Os tipos aceitos são `cost`, `revenue` e `attributed_value`. O valor aceita sinal para que uma
correção seja registrada por uma nova entrada compensatória, preservando o histórico append-only.
SQLite persiste o decimal como texto canônico sem arredondamento; PostgreSQL usa
`NUMERIC(30,10)`. Repetir a chave com os mesmos dados retorna a entrada existente e qualquer
divergência é rejeitada. A saída redigida não repete categoria, fonte nem chave idempotente. Estas
entradas representam somente observações: não há gasto, cobrança, pagamento, transferência ou
promessa financeira implementada.

Uma condição operacional local pode ser aberta por uma ocorrência idempotente e tratada pelo
operador sem notificação externa:

```powershell
.venv\Scripts\automation-foundry record-alert --automation-slug platform-smoke --deduplication-key "service:redis:offline" --idempotency-key "service:redis:offline:20260723T1200" --title "Redis is offline" --summary "The local broker did not accept a connection." --severity error --source service-health
.venv\Scripts\automation-foundry set-alert --alert-id 1 --acknowledge --actor "local-owner" --reason "investigating locally"
.venv\Scripts\automation-foundry set-alert --alert-id 1 --resolve --actor "local-owner" --reason "local service recovered"
```

Repetir a chave da ocorrência não aumenta a contagem. Uma nova ocorrência posterior ao
reconhecimento ou à resolução reabre o alerta; uma ocorrência atrasada anterior ao tratamento é
rejeitada. O summary fica no banco local e não é repetido na saída da CLI. Esta fatia ainda não
coleta health checks nem envia email, mensagem ou webhook.

`automation-foundry doctor` é a execução canônica e sem efeitos colaterais da Fase 1. Use
`doctor --json` para saída estruturada e `doctor --services` somente quando Redis e Ollama locais
devem estar ativos. O Redis pode ser iniciado com `docker compose up -d redis`.

Depois de `alembic upgrade head`, a primeira visão do control plane é iniciada com:

```powershell
.venv\Scripts\automation-foundry dashboard
```

O host default é `127.0.0.1`; configuração e cabeçalho `Host` não-loopback são rejeitados. As páginas
consultam o banco e não dependem de Redis, Ollama, Celery ou GPU. A única rota mutável solicita
cancelamento de run com confirmação explícita, token CSRF por processo e evento persistido.
Totais financeiros são calculados com `Decimal` em Python para preservar a precisão do SQLite e
permanecem separados por moeda, sem conversão cambial.

## Baseline observado neste checkpoint

O ambiente fornecido possui Python 3.12.13. A primeira coleta, antes da instalação das
dependências do projeto, produziu:

- `python -m pytest -q`: interrompido na coleta por ausência de `sqlalchemy` e `httpx`;
- `python -m ruff check .`: não executado porque o módulo `ruff` não estava instalado;
- `python -m mypy src`: não executado porque o módulo `mypy` não estava instalado.

Após criar `.venv` e instalar `.[dev]` sem os extras `ai`:

- `python -m pip install -e ".[dev]"`: concluído; a primeira tentativa foi bloqueada pela rede da
  sandbox e a repetição com acesso autorizado concluiu a instalação;
- `python -m pytest -q`: **196 passed**;
- `python -m ruff check .`: encontrou 7 ocorrências mecânicas preexistentes; o autofix removeu
  imports não usados, ordenou imports e simplificou um context manager; a repetição terminou com
  **All checks passed**;
- `python -m mypy src`: **Success: no issues found in 50 source files**;
- `python -m pip check`: **No broken requirements found**;
- validação estrutural de `pyproject.toml` e `docker-compose.yml` por `tomllib` e YAML:
  concluída, incluindo metadata, entry point `src.cli:main` e nome do container;
- `automation-foundry doctor`: concluído sem serviços externos, com checks estáticos aprovados e
  sondas de Redis/Ollama explicitamente marcadas como `skip`;
- `automation-foundry doctor --json`: concluído com código 0 e payload redigido;
- `automation-foundry doctor --services`: código 1 esperado neste ambiente de viagem, com Redis e
  Ollama reportados como indisponíveis em loopback;
- `docker compose config`: bloqueado porque o executável Docker não está disponível neste
  ambiente.
- migrations Alembic em SQLite temporário: `upgrade head`, `alembic check` e roundtrip das revisions
  de artifacts, schedules, métricas, alertas e ledger concluídos; somente as quinze tabelas compartilhadas e `alembic_version`
  foram criadas.

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

Definir uma projeção de revisão segura e específica para o payload protegido é pré-requisito para
habilitar approval positiva pela UI sem criar aprovação às cegas. `Experiment`, o dispatcher de
schedules e o caminho PostgreSQL concorrente permanecem pendentes; concorrência, Redis, Celery Beat
e hard time limits continuam dependentes do computador de casa.
