# Estado real da implementação

**Checkpoint:** 2026-08-10

**Escopo:** Fase 1 e fatias locais verificáveis das Fases 2, 3 e 5

**Branch:** `chore/project-skills`

## Veredito

O repositório possui um kernel local parcial, testável e observável e um supervisor Windows para o
runtime compartilhado. A geração de roteiro A1, a configuração, o banco, o dispatch Celery, os
schedules e o control plane inicial têm código real. O kernel compartilhado persiste `Automation`,
`Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, `MetricPoint`, `LedgerEntry`, `Alert`, controles
de execução e cada transição de estado. O dashboard permite controles locais limitados e mostra um
snapshot redigido da saúde. As demais etapas dos módulos continuam placeholders ou planejadas.

## Classificação por área

| Área | Estado | Evidência e limite atual |
| --- | --- | --- |
| Identidade técnica | Implementado | `pyproject.toml`, `.env.example`, `src/core/config.py` e `docker-compose.yml` usam `automation-foundry` nos identificadores ativos. “Content Engine” permanece como nome legítimo do módulo. |
| Configuração | Parcial | `src/core/config.py` implementa settings tipados, leitura de `.env` e defaults locais. Campos de credenciais existem, mas criptografia, validação operacional e adapters consumidores ainda não existem. |
| Banco e sessão | Implementado no caminho local atual | `src/core/database.py` cria engine e sessão async com commit/rollback. PostgreSQL usa `asyncpg`, pool limitado, pre-ping e timeouts de conexão/comando; somente URLs em loopback são aceitas. SQLite permanece como default sem `.env`, bootstrap de processo único e backend dos testes. O dispatch concorrente foi validado em PostgreSQL. |
| Modelos de domínio | Parcial | `src/core/models.py` implementa `Channel`, `Video`, `Job`, `Metric`, `Cost`, `Topic`, `Alert` e `ABVariant`. O schema é específico do Content Engine e não contém os contratos genéricos da Fase 2. |
| Kernel compartilhado | Parcial | `src/platform/models.py` implementa `Automation`, `Run`, `StepRun`, `RunDispatch`, `Approval`, `Artifact`, `Schedule`, `ScheduleOccurrence`, `MetricPoint`, `Experiment`, `LedgerEntry`, `Alert`, `HealthCondition`, controles persistidos e históricos append-only. Cada horário de schedule possui resultado `pending`, `published` ou `skipped`; dispatch possui identidade estável, tentativas de publicação e claim/lease. |
| Migrations | Implementado no baseline atual | `alembic.ini`, `alembic/env.py` e dezessete revisions incrementais formam um caminho verificável para as tabelas compartilhadas e as oito tabelas legadas do Content Engine. `platform_alerts` evita colisão com `alerts`, e os roundtrips são testados. Evoluções futuras de schema ainda exigem novas migrations. |
| Content Engine — A1 | Implementado | `src/pipeline/script_gen.py` contém geração em quatro chamadas, retry limitado, saneamento de saída e persistência local de roteiro/narração. `tests/pipeline/test_script_gen.py` cobre o comportamento com Ollama mockado. |
| Content Engine — mídia e publicação | Placeholder | `src/pipeline/tts.py`, `visuals.py`, `captions.py`, `assembly.py` e `upload.py` estão vazios. Nenhuma publicação real foi implementada. |
| Orquestração e filas | Parcial | `src/core/celery_app.py` declara exclusivamente `gpu`, `cpu` e `io`, usa mensagens JSON persistentes, late ack, prefetch 1, publish retry limitado e timeouts configurados. `RunDispatch` protege a entrega e `ScheduleOccurrence` protege cada horário previsto. Beat publica ticks de schedule e health na fila IO. O entrypoint combina PID file e named mutex no Windows; uma segunda aquisição do mutex é recusada. |
| CLI operacional | Parcial | `src/cli.py` expõe diagnóstico, dashboard operacional, execução inline, `enqueue-run`, controles, approvals, artifacts, schedules, métricas, ledger e alertas. `automation-foundry-runtime start/stop/status` opera a topologia fixa; os entrypoints de worker e Beat continuam disponíveis para diagnóstico. A CLI local ainda não autentica criptograficamente o actor. |
| Control plane | Parcial | `src/dashboard/main.py`, `service.py` e os templates implementam visão geral, detalhe da run e saúde operacional redigida. O dashboard mostra experiments sem hipótese ou motivos privados e permite atribuir um disparo manual a um experiment `running`. Um registro fail-closed permite disparo e retry somente para executores declarados; por enquanto apenas o smoke local está registrado. Cancelamento, kill switch e approvals usam POST com CSRF, Host loopback e confirmação. Ainda não há atualização HTMX interativa. A identidade do actor local ainda não é autenticada. |
| Inteligência | Placeholder | `src/intelligence/trends.py`, `competitors.py` e `similarity.py` estão vazios. |
| Engajamento | Placeholder | `src/engagement/comments.py` está vazio. |
| Operações | Parcial | `src/operations/health.py` coleta CPU, memória, disco, GPU/VRAM, banco, Redis, Beat e workers `gpu`/`cpu`/`io` com timeout e saída redigida. `health_alerts.py` persiste somente consecutividade e alertas allowlisted: duas falhas abrem/atualizam e duas recuperações resolvem; `skip` é inconclusivo. Não há histórico de métricas nem notificação externa. `seo.py` e `repurpose.py` continuam vazios. |
| Receita e experimentos | Parcial | O contrato compartilhado de Experiment e sua atribuição de runs estão implementados. `src/revenue/aggregator.py`, `profit.py` e `ab_testing.py` continuam vazios; não há análise estatística nem decisão automática de vencedor. |
| Extras e ações externas | Placeholder | `src/extras/affiliate.py`, `outreach.py` e `x_bot.py` estão vazios. Não há envio, gasto ou ação externa implementada. |
| Trading Research Lab | Planejado | O domínio aparece em `ARQUITETURA.md` e `ROADMAP.md`, mas não possui implementação. Trading com dinheiro real permanece fora do escopo. |
| Infraestrutura local | Parcial | `docker-compose.yml` define PostgreSQL 17 e Redis com volumes, health checks e portas publicadas somente em `127.0.0.1`. O supervisor Windows inicia três workers host, Beat e dashboard, reivindica somente serviços Compose que estavam parados e nunca remove volumes. Ollama permanece fora do Compose. |
| Testes | Parcial | A suíte cobre publicação, falha de broker, claim, lease expirada, ownership, duplicatas, redaction de health e lifecycle do supervisor. Um ciclo isolado em Windows real validou mutex, processos ocultos, estado, sinal externo, shutdown cooperativo e fallback Job Object sem tocar Docker ou banco. Integrações opt-in validam PostgreSQL, Redis/Celery, dispatch, schedule e três workers. Restart durante task, modelos e carga GPU ainda não foram validados. |

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

O schema gerenciado da Fase 2 e do Content Engine é atualizado explicitamente com:

```powershell
docker compose up -d postgres redis
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m alembic check
```

Antes de iniciar o Compose, copie `.env.example` para o `.env` local, defina
`POSTGRES_PASSWORD` e substitua `SET_IN_LOCAL_ENV` na `DATABASE_URL` pelo mesmo valor com
URL-encoding. O arquivo `.env` permanece ignorado pelo Git. Sem `.env`, o código usa SQLite para
bootstrap de processo único; esse fallback não deve ser usado por workers concorrentes.

Com o `.env` configurado para PostgreSQL local, use o supervisor em foreground como caminho
operacional canônico. Ele valida a configuração, inicia PostgreSQL e Redis quando necessário,
executa migrations e abre workers, Beat e dashboard como processos ocultos:

```powershell
.venv\Scripts\automation-foundry-runtime start
# Em outro terminal:
.venv\Scripts\automation-foundry-runtime status --json
.venv\Scripts\automation-foundry-runtime stop
```

O supervisor permanece em foreground para tornar falhas visíveis. Estado e logs operacionais ficam
em `STORAGE_ROOT/runtime`; o status público não expõe PIDs. No shutdown ele sinaliza cada processo,
aplica timeout e usa Job Object como fallback. Serviços Compose já ativos não são parados, e o
supervisor nunca executa `docker compose down` nem remove volumes. Os entrypoints individuais
continuam disponíveis para diagnóstico. Eles não expõem opções de pool ou concorrência: no Windows,
cada worker usa `solo` e concorrência 1. Isso garante GPU serializada e permite paralelismo apenas
entre os três processos.
Os tasks Celery registrados são os probes sem efeito externo, o executor de dispatch durável e os
ticks de schedules e reconciliação de health. Beat apenas publica os ticks; horários, ocorrências,
consecutividade e alertas continuam no PostgreSQL.
Runs publicadas por `enqueue-run` usam identidade estável, claim/lease no PostgreSQL e replay seguro;
o executor inline permanece disponível para diagnóstico local.

Esses comandos gerenciam as vinte e uma tabelas compartilhadas: `automations`, `runs`,
`run_transitions`, `run_dispatches`, `run_dispatch_events`, `step_runs`, `step_run_transitions`, `control_events`, `approvals`,
`approval_events`, `artifacts`, `schedules`, `schedule_events`, `schedule_occurrences`, `experiments`, `experiment_events`, `metric_points`, `ledger_entries`, `platform_alerts`,
`platform_alert_events` e `health_conditions`;
e as oito tabelas legadas do Content Engine: `channels`, `videos`,
`jobs`, `metrics`, `costs`, `topics`, `alerts` e `ab_variants`.

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
.venv\Scripts\automation-foundry kill-switch --automation-slug platform-smoke --enable --actor "local-owner" --reason "maintenance"
.venv\Scripts\automation-foundry kill-switch --automation-slug platform-smoke --disable --actor "local-owner" --reason "review completed"
.venv\Scripts\automation-foundry cancel-run --run-id 1 --reason "operator request"
```

Um gate para o payload atual da run usa:

```powershell
.venv\Scripts\automation-foundry request-approval --run-id 1 --idempotency-key "publish:1" --action publish --summary "Review private upload" --review '{"artifact":"Video #1","visibility":"Private"}'
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
persistidas em UTC. O tick cria uma ocorrência única por horário, aplica `allow_overlap` e
`misfire_grace_seconds`, avança o calendário no banco e recupera publicação pendente em tick futuro.

Um experimento mensurável nasce em draft e exige transição auditada antes de receber runs:

```powershell
.venv\Scripts\automation-foundry create-experiment --automation-slug platform-smoke --key smoke-v2 --name "Smoke V2" --hypothesis "A variante reduz falhas." --primary-metric smoke.success_rate --unit ratio --control baseline --candidate v2 --actor "local-owner" --reason "hipótese revisada"
.venv\Scripts\automation-foundry set-experiment --experiment-id 1 --start --actor "local-owner" --reason "iniciar medição"
```

As transições suportam start, pause, retomada, conclusão e cancelamento. Estados terminais são
imutáveis. O dashboard redige hipótese, actors e motivos; nenhuma análise estatística ou escolha
automática de vencedor foi implementada.

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
rejeitada. O summary fica no banco local e não é repetido na saída da CLI. O monitor automático
usa somente títulos e resumos allowlisted: duas observações `degraded/fail` abrem o alerta e duas
`pass` o resolvem. Nenhum email, mensagem ou webhook é enviado.

`automation-foundry doctor` é a execução canônica e sem efeitos colaterais da Fase 1. Use
`doctor --json` para saída estruturada e `doctor --services` somente quando Redis e Ollama locais
devem estar ativos. O Redis pode ser iniciado com `docker compose up -d redis`.

Depois de `alembic upgrade head`, a primeira visão do control plane é iniciada com:

```powershell
.venv\Scripts\automation-foundry dashboard
```

O host default é `127.0.0.1`; configuração e cabeçalho `Host` não-loopback são rejeitados. Visões de
estado consultam o banco; `/health` faz probes limitados e somente leitura dos recursos locais.
Controles mutáveis de run, kill switch e approval exigem confirmação explícita e token CSRF por
processo. Totais financeiros são calculados com `Decimal` em Python para preservar a precisão do
SQLite e permanecem separados por moeda, sem conversão cambial.

## Baseline observado neste checkpoint

O ambiente fornecido possui Python 3.12.13. A primeira coleta, antes da instalação das
dependências do projeto, produziu:

- `python -m pytest -q`: interrompido na coleta por ausência de `sqlalchemy` e `httpx`;
- `python -m ruff check .`: não executado porque o módulo `ruff` não estava instalado;
- `python -m mypy src`: não executado porque o módulo `mypy` não estava instalado.

Após criar `.venv` e instalar `.[dev]` sem os extras `ai`:

- `python -m pip install -e ".[dev]"`: concluído; a primeira tentativa foi bloqueada pela rede da
  sandbox e a repetição com acesso autorizado concluiu a instalação;
- `python -m pytest -q`: **280 passed, 6 skipped**; os skips são integrações opt-in que exigem
  PostgreSQL e/ou Redis descartáveis;
- `python -m ruff check .`: encontrou 7 ocorrências mecânicas preexistentes; o autofix removeu
  imports não usados, ordenou imports e simplificou um context manager; a repetição terminou com
  **All checks passed**;
- `python -m mypy src`: **Success: no issues found in 66 source files**;
- `python -m pip check`: **No broken requirements found**;
- validação estrutural de `pyproject.toml` e `docker-compose.yml` por `tomllib` e YAML:
  concluída, incluindo metadata, entry point `src.cli:main` e nome do container;
- `automation-foundry doctor`: concluído sem serviços externos, com checks estáticos aprovados e
  sondas de Redis/Ollama explicitamente marcadas como `skip`;
- `automation-foundry doctor --json`: concluído com código 0 e payload redigido;
- `automation-foundry doctor --services`: código 1 esperado neste ambiente de viagem, com Redis e
  Ollama reportados como indisponíveis em loopback;
- `docker compose config --quiet`: concluído com senha efêmera fornecida apenas ao processo de
  validação; PostgreSQL e Redis publicam portas somente em loopback.
- migrations Alembic em SQLite temporário: `upgrade head`, `alembic check` e roundtrip das revisions
  de artifacts, schedules, experiments, métricas, alertas, ledger e baseline legado concluídos; as
  vinte e uma tabelas compartilhadas, as oito tabelas do Content Engine e `alembic_version` foram criadas.
- PostgreSQL 17 descartável: todas as migrations e `alembic check` concluídos; duas runs em sessões
  concorrentes foram persistidas e o replay da mesma chave retornou a run original. O container e
  seus dados efêmeros foram removidos após o teste. A repetição usa `TEST_POSTGRESQL_URL` com
  `pytest -m postgresql` e exige um banco dedicado descartável.
- Redis 7 descartável + Celery 5.6: workers reais `solo` consumiram sequencialmente as filas
  `gpu`, `cpu` e `io`; cada probe retornou somente o nome da própria fila e um nonce de teste. O
  container, mensagens e resultados efêmeros foram removidos. A repetição usa
  `TEST_REDIS_URL`, `TEST_REDIS_RESULT_URL` e `pytest -m redis`.
- PostgreSQL 17 + Redis 7 + Celery 5.6 descartáveis: uma run foi preparada e publicada, o worker
  IO adquiriu a lease e concluiu run/step/dispatch; a repetição da mesma entrega não criou outro
  step. Os containers e dados efêmeros foram removidos após o teste.
- Schedule real: o worker IO consumiu `schedule.tick`, criou ocorrência e run com trigger
  `schedule`, publicou o dispatch e concluiu a execução. Misfire, bloqueio de sobreposição e retry
  de broker possuem testes locais. O PID lock isolado do Celery não recusou a segunda aquisição no
  Windows; o entrypoint foi endurecido com named mutex, cuja exclusão mútua passou no teste.
- Health real com Redis 7 descartável: três workers simultâneos, um para cada fila `gpu`, `cpu` e
  `io`, foram contados sem expor seus nomes. O container e os resultados efêmeros foram removidos.
- Snapshot desta máquina: CPU, memória, disco, SQLite e uma NVIDIA GeForce RTX 3090 de 24 GiB
  foram detectados; Redis e Beat parados apareceram como falha e a inspeção de workers foi pulada
  após a falha do Redis. O snapshot concluiu em 1,51 s sem retornar URL, hostname ou segredo.
- Reconciliação real em memória: a primeira falha não abriu alerta e a segunda abriu apenas Redis e
  Beat, os serviços efetivamente parados; oito condições foram persistidas sem endpoint ou segredo.
- PostgreSQL 17 descartável: a consecutividade sobreviveu a sessões separadas, abriu o alerta após
  duas falhas e o resolveu após dois resultados saudáveis. A revision 0017 e o downgrade/upgrade
  também passaram; o container e seus dados efêmeros foram removidos.
- Supervisor Windows isolado: mutex singleton, evento global de stop, Job Object kill-on-close,
  processo oculto, arquivo de estado, proteção contra reutilização de PID e shutdown cooperativo
  passaram em ciclo real. Infraestrutura e migrations foram substituídas no teste; Docker, banco e
  volumes locais não foram tocados.

## Verificações bloqueadas ou adiadas

- **Computador de casa:** CPU, RAM e disco responderam ao snapshot; o lifecycle isolado do
  supervisor passou. O boot completo real foi recusado corretamente pelo preflight porque a
  configuração ativa ainda usa SQLite; depende de um `.env` PostgreSQL local válido.
- **GPU:** RTX 3090 e VRAM responderam ao `nvidia-smi`; CUDA, modelos, carga prolongada e fila GPU
  com trabalho real permanecem pendentes.
- **Serviços locais:** validar Ollama, Redis, o volume PostgreSQL persistente e recuperação após
  indisponibilidade no computador de casa. A integração PostgreSQL efêmera já foi validada.
- **Timeouts de worker no Windows:** soft/hard limits estão configurados, mas o pool `solo` não
  oferece todas as garantias de timeout dos pools baseados em processos. O task wrapper mantém seu
  timeout assíncrono; preempção de código síncrono bloqueante continua pendente de validação local.
- **Credenciais:** validar somente em modo opt-in as APIs oficiais de Google/YouTube, Reddit,
  bancos de mídia, X e demais provedores. Nenhuma credencial deve entrar no Git.
- **Ações externas:** publicação, mensagens, gastos, exclusões materiais e ações financeiras
  continuam sem implementação. O kill switch genérico existe; approval e gates específicos de
  domínio ainda são obrigatórios antes de qualquer teste externo.

## Próxima fatia recomendada

Criar backup e restore coordenados de PostgreSQL, configuração sem secrets e artefatos, com
manifesto verificável e sem exclusão implícita. Hard time limits, reinício durante task e recuperação
do volume persistente continuam dependentes de validação local controlada.
