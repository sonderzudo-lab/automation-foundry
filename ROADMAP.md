# Roadmap do Automation Foundry

Este roadmap ordena trabalho por dependÃªncia e evidÃªncia, nÃ£o por prazo. Uma fase termina apenas quando seu portÃ£o Ã© verificÃ¡vel.

## Legenda

- `[x]` concluÃ­do na branch de trabalho.
- `[ ]` pendente.
- `bloqueado local` exige o computador de casa, GPU, credenciais ou revisÃ£o manual.

## PrincÃ­pios de prioridade

1. Primeiro tornar uma execuÃ§Ã£o segura e observÃ¡vel.
2. Depois concluir um mÃ³dulo que produza valor ponta a ponta.
3. SÃ³ entÃ£o adicionar novos domÃ­nios.
4. Automatizar aÃ§Ãµes externas apenas apÃ³s o fluxo manual ser confiÃ¡vel.
5. Medir custo, qualidade e valor antes de falar em escala.

## Fase 0 â€” Fundamentos do projeto

Objetivo: alinhar nome, regras e direÃ§Ã£o sem alterar prematuramente a implementaÃ§Ã£o testada.

- [x] Renomear o repositÃ³rio para `automation-foundry`.
- [x] Criar branch de estruturaÃ§Ã£o.
- [x] Criar skills gerais e de domÃ­nio para Codex/Cursor e Claude Code.
- [x] Desabilitar a atribuiÃ§Ã£o automÃ¡tica do Claude no projeto.
- [x] Definir arquitetura local-first e roadmap modular.
- [ ] Revisar e fazer merge da branch apÃ³s inspeÃ§Ã£o do usuÃ¡rio.

**PortÃ£o:** `AGENTS.md`, `ARQUITETURA.md`, `ROADMAP.md` e skills contam a mesma histÃ³ria; `main` permanece inalterada atÃ© aprovaÃ§Ã£o.

## Fase 1 â€” Baseline e identidade tÃ©cnica

Objetivo: fazer a base atual representar Automation Foundry e obter um baseline reproduzÃ­vel.

- [x] Renomear package metadata, comando CLI, banco default, container e variÃ¡veis que ainda usam `content-engine`.
- [x] Catalogar arquivos vazios, stubs e implementaÃ§Ã£o real.
- [x] Executar testes existentes e registrar o baseline.
- [x] Corrigir apenas falhas que bloqueiem o baseline, sem reescrever A1.
- [x] Atualizar `.env.example` para defaults locais seguros e loopback.
- [x] Definir comandos Ãºnicos para setup, lint, testes e execuÃ§Ã£o. A execuÃ§Ã£o da Fase 1 usa `automation-foundry doctor`; o dashboard permanece na Fase 3.
- [ ] Confirmar versÃµes e specs no computador de casa. `bloqueado local`

**PortÃ£o:** um checkout limpo instala as dependÃªncias de desenvolvimento, executa os testes existentes e identifica honestamente o que estÃ¡ ou nÃ£o implementado.

## Fase 2 â€” Kernel da plataforma

Objetivo: executar uma automaÃ§Ã£o mÃ­nima usando contratos genÃ©ricos e estado durÃ¡vel.

- [x] Introduzir os conceitos `Automation`, `Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, `MetricPoint`, `Experiment`, `LedgerEntry` e `Alert`.
- [x] Criar migrations e separar tabelas compartilhadas das tabelas do Content Engine. Dezenove migrations incrementais cobrem o kernel compartilhado, o dispatch durÃ¡vel, continuacoes aprovadas e o baseline legado; `platform_alerts` e `alerts` permanecem domÃ­nios distintos.
- [x] Adotar PostgreSQL para o caminho concorrente; manter SQLite nos testes. O runtime local usa `asyncpg`, pool limitado e Compose preso a loopback; migrations, sessÃµes concorrentes e replay idempotente foram validados em PostgreSQL 17 descartÃ¡vel.
- [x] Configurar Celery + Redis e filas `gpu`, `cpu`, `io`. A topologia fail-closed, os trÃªs workers seriais e o roteamento foram validados contra Redis real; probes efÃªmeros e o task de dispatch durÃ¡vel estÃ£o registrados.
- [x] Implementar dispatch durÃ¡vel de automaÃ§Ãµes. A run e sua identidade estÃ¡vel sÃ£o persistidas antes da publicaÃ§Ã£o; falhas de broker permanecem reenviÃ¡veis, claims usam lease no PostgreSQL e entregas duplicadas nÃ£o repetem o step. O fluxo foi validado com PostgreSQL, Redis e worker IO reais.
- [x] Implementar task wrapper idempotente com retries limitados, timeout, backoff exponencial e transiÃ§Ãµes persistidas em processo Ãºnico.
- [x] Implementar pedidos de cancelamento e kill switch persistentes, auditÃ¡veis e consultados pelo wrapper em processo Ãºnico.
- [x] Registrar uma automaÃ§Ã£o de exemplo e executar um step manual observÃ¡vel em processo Ãºnico, sem efeito externo.
- [x] Registrar metric points decimais, idempotentes e atribuÃ­veis, sem coleta ou efeito externo.
- [x] Registrar ledger entries decimais, append-only, idempotentes e atribuÃ­veis, sem pagamentos ou aÃ§Ãµes financeiras.
- [x] Registrar alertas locais deduplicados, auditados e sem notificaÃ§Ãµes externas.

**PortÃ£o:** uma run manual percorre `queued â†’ running â†’ succeeded/failed`, sobrevive a retry e deixa evidÃªncia completa no banco.

## Fase 3 â€” Control plane local

Objetivo: controlar a plataforma sem depender do terminal para a operaÃ§Ã£o normal.

- [x] Criar FastAPI ligado a loopback por configuraÃ§Ã£o validada, com `127.0.0.1` como default.
- [ ] Construir dashboard Jinja/HTMX para modulos, runs, steps e alertas. A visao read-only mostra automacoes, runs recentes, alertas ativos, schedules ordenados e indicadores por automacao sobre ate 50 resultados succeeded/failed recentes; runs canceladas ou abertas nao entram na taxa de sucesso, e a duracao media usa apenas timestamps validos. O detalhe de run mostra status, trigger, duracao, steps/tentativas/filas e evidencias vinculadas com redaction; status e steps usam um fragmento HTMX `no-store`, servido localmente, que consulta apenas runs nao terminais e degrada para a pagina completa sem JavaScript. Interacoes HTMX nas demais visoes, connectors, freshness e qualidade de dados permanecem pendentes.
- [ ] Adicionar fila de aprovacoes. A pagina inicial liga pendencias redigidas ao detalhe da run e mostra sua idade calculada no servidor; o detalhe permite rejeicao imutavel e aprovacao positiva com actor, motivo, confirmacao e protecao CSRF. Aprovacao positiva so aparece para uma projecao limitada e validada do payload; registros legados sem esse contexto permanecem bloqueados. O A1 possui revisao integral `no-store` do bundle validado por SHA-256 e so conclui depois da decisao; alteracao posterior do arquivo recusa a aprovacao e reverte a transacao. Resolver diretamente pela fila ainda permanece pendente.
- [ ] Permitir iniciar, cancelar, retentar, habilitar/desabilitar e acionar kill switch. O dashboard dispara executores registrados por um contrato fail-closed, cria retry como nova run ligada a uma falha retryable e permite cancelamento, habilitacao administrativa e kill switch com confirmacao, protecao CSRF e auditoria. `Automation.enabled` bloqueia novas runs e o inicio das enfileiradas sem interromper runs ativas; o kill switch permanece um controle operacional separado. O smoke inline e o Content Engine A1 em background estao registrados; os demais fluxos de dominio ainda precisam aderir ao contrato.
- [x] Adicionar schedules persistidos, desabilitados por padrÃ£o e com mudanÃ§as auditadas.
- [x] Integrar os schedules ao Celery Beat singleton e validar dispatch idempotente. O tick no worker IO usa ocorrÃªncias Ãºnicas no PostgreSQL, aplica misfire/sobreposiÃ§Ã£o e reutiliza o dispatch durÃ¡vel; o fluxo real PostgreSQL + Redis + worker passou. O entrypoint Beat combina PID file com named mutex no Windows, cuja segunda aquisiÃ§Ã£o foi recusada em teste.
- [x] Mostrar saÃºde de CPU, RAM, GPU/VRAM, disco, banco, Redis, workers e Beat em snapshot redigido. O dashboard nÃ£o revela endpoints ou nomes de workers; integraÃ§Ã£o com trÃªs workers reais e o probe local da RTX 3090 passaram.
- [ ] Mostrar custos e resultados atribuiveis. Totais exatos do ledger aparecem globalmente e por automacao e moeda, sem conversao, e observacoes vinculadas aparecem no detalhe da run. Analise de resultado e indicadores derivados permanecem pendentes.

**PortÃ£o:** pelo dashboard, o usuÃ¡rio dispara e acompanha uma run, resolve uma aprovaÃ§Ã£o, diagnostica uma falha e desabilita o mÃ³dulo.

## Fase 4 â€” Content Engine como primeiro mÃ³dulo completo

Objetivo: transformar a implementaÃ§Ã£o existente em uma automaÃ§Ã£o editorial segura e demonstrÃ¡vel.

- [x] Integrar A1 ao contrato de runs sem perder seus testes atuais. O dashboard valida pauta, persona, formato e aberturas recentes, prepara dispatch duravel na fila `gpu` e acompanha run/step. O executor usa a geracao existente sem altera-la, grava bundle JSON atomico e revisavel, registra artifact com retencao, metrica e custo externo zero e aguarda approval editorial ligada ao hash do arquivo. A revisao integral omite o contexto privado do prompt; aprovacao conclui a run de forma idempotente, rejeicao cancela e bundle alterado bloqueia e reverte a decisao. Idempotencia, falha retryable, falha parcial de evidencia, retry explicito, cancelamento e entrega duplicada possuem testes. Ollama e GPU reais continuam `bloqueado local`.
- [ ] Implementar TTS comercialmente compativel. O contrato A2 ja consome somente bundle A1 aprovado, roda como step `gpu` ordinal 2, valida e registra WAV atomico, duracao, energia estimada, proveniencia/licenca e custo externo zero; retry, falha de audio, falha parcial de evidencia, continuation dispatch e entrega duplicada usam adapter fake. A auditoria separou engine/pesos Apache-2.0, eSpeak NG externo GPL-3.0-or-later e vozes PT-BR sem proveniencia individual documentada. O adapter `kokoro_quality_test` fixa pacote 0.9.4, revisao, peso e voz, exige CUDA/eSpeak, limita chunks e persiste o escopo nao monetizado; `disabled` permanece o default. Escuta humana das tres vozes na RTX 3090 e decisao explicita sobre o risco residual continuam `bloqueado local`.
- [ ] Implementar visuais, legendas e montagem em steps separados. A3 verifica bundle/audio, exige proveniencia/licenca comercial por asset e registra imagens + manifesto. Além dos fakes, `local_assets_quality_test` permite importar PNGs escolhidos pelo operador em raiz contida, com quantidade exata, SHA-256 e direitos explícitos por item, na fila `io`; o default segue `disabled`, não há download, FLUX.1 continua bloqueado e o manifesto não substitui revisão humana. A4 verifica WAV/narração, timestamps, idioma, similaridade e direitos comerciais e publica ASS; `approved_text_timing_quality_test` roda em `cpu` e estima intervalos determinísticos a partir do texto aprovado e da duração real, sem modelo, mas não substitui alinhamento ou revisão humana. A5 reconcilia todos os artifacts, roda na fila `cpu`, valida estrutura/tracks/resolução/duração do MP4 e registra export local, métricas, energia e custo. Além dos fakes, `ffmpeg_quality_test` fixa os hashes do build externo Gyan 8.0.1 GPLv3, usa subprocess sem shell e ffprobe; um short sintético H.264/AAC reproduzível passou com legenda visualmente confirmada. Os defaults continuam `disabled`; narração real, pacote visual real revisado, sincronismo revisado, long e política de instalação/redistribuição permanecem pendentes.
- [ ] Implementar similaridade e bloqueio editorial. O contrato A6 local já verifica roteiro/MP4, compara lexicalmente contra até 20 runs anteriores aprovadas no mesmo escopo de automação, registra relatório e métricas e bloqueia com alerta quando o score máximo atinge 0,85. Duplicata, conteúdo diferente, adulteração histórica, replay terminal e falha parcial possuem testes. Escopo explícito por canal, detecção semântica de paráfrases e um eventual override humano dedicado permanecem pendentes; o bloqueio atual é estrito e não publica nada.
- [ ] Exigir aprovaÃ§Ã£o de pauta, Ã¢ngulo, thumbnail e publicaÃ§Ã£o.
- [ ] ComeÃ§ar com export local; depois upload privado por API oficial.
- [ ] Coletar analytics, custo e resultado por conteÃºdo.
- [ ] Validar modelos, FFmpeg, Ollama e consumo de VRAM. `bloqueado local`

**PortÃ£o:** uma pauta manual gera um pacote de vÃ­deo revisÃ¡vel; nada Ã© publicado sem aprovaÃ§Ã£o; falhas e custos aparecem no dashboard.

## Fase 5 â€” OperaÃ§Ã£o domÃ©stica confiÃ¡vel

Objetivo: deixar o sistema rodar no computador principal com recuperaÃ§Ã£o previsÃ­vel.

- [x] Definir startup e shutdown seguros no Windows. O supervisor singleton executa preflight e migrations, inicia a topologia fixa em processos ocultos, usa shutdown cooperativo por eventos nomeados e Job Object como fallback, protege o status contra reutilizacao de PID e para somente os servicos Compose que iniciou, sem remover volumes. O ciclo completo isolado passou em Windows real; a ativacao com o volume PostgreSQL local depende de configurar o `.env`.
- [x] Implementar health checks locais redigidos e com timeout limitado.
- [x] Transformar degradaÃ§Ãµes persistentes de health em alertas locais deduplicados. Duas observaÃ§Ãµes degradadas abrem ou atualizam o alerta e duas saudÃ¡veis o resolvem; `skip` nÃ£o altera o tratamento. Estado e eventos ficam no PostgreSQL, a task roda na fila `io` e nÃ£o envia notificaÃ§Ãµes externas.
- [x] Criar backup e restore de banco, configuraÃ§Ã£o e artefatos. O bundle versionado publica snapshot, configuraÃ§Ã£o allowlisted, artefatos e manifesto SHA-256 atomicamente. Restore exige confirmaÃ§Ã£o exata, runtime parado, storage vazio e backup automÃ¡tico prÃ©vio; SQLite usa substituiÃ§Ã£o atÃ´mica e PostgreSQL usa transaÃ§Ã£o Ãºnica. Um ciclo real passou em PostgreSQL 17 e volume Compose descartÃ¡veis; o volume persistente domÃ©stico continua `bloqueado local`.
- [ ] Definir retenÃ§Ã£o e limpeza de storage.
- [ ] Testar reinÃ­cio durante task, perda de Redis, falta de disco e indisponibilidade do Ollama.
- [ ] Documentar operaÃ§Ã£o e troubleshooting reais. `bloqueado local`

**PortÃ£o:** apÃ³s reinÃ­cio ou falha simulada, o estado permanece consistente, nÃ£o hÃ¡ execuÃ§Ã£o duplicada e existe recuperaÃ§Ã£o documentada.

## Fase 6 â€” Segundo mÃ³dulo orientado a valor

Objetivo: provar que Automation Foundry Ã© plataforma, nÃ£o apenas um pipeline de vÃ­deo.

Avaliar candidatos por valor esperado, qualidade de dados, risco, esforÃ§o e manutenÃ§Ã£o:

1. inteligÃªncia de mercado e concorrentes;
2. monitor de preÃ§os, promoÃ§Ãµes ou oportunidades;
3. pesquisa e qualificaÃ§Ã£o de leads com rascunhos aprovÃ¡veis;
4. geraÃ§Ã£o recorrente de relatÃ³rios e briefs.

- [ ] Selecionar um Ãºnico candidato com hipÃ³tese e mÃ©trica de sucesso.
- [ ] Especificar pelo contrato `$build-automation-module`.
- [ ] Implementar primeiro uma execuÃ§Ã£o manual.
- [ ] Integrar estado, aprovaÃ§Ãµes, custos e mÃ©tricas ao dashboard.
- [ ] Avaliar o experimento antes de automatizar o schedule.

**PortÃ£o:** o segundo mÃ³dulo reutiliza o kernel sem copiar o control plane e demonstra valor mensurÃ¡vel em um caso real.

## Fase 7 â€” Trading Research Lab

Objetivo: construir um projeto quantitativo sÃ©rio para pesquisa e portfÃ³lio, sem dinheiro real.

- [ ] Definir fontes, universos, periodicidade e schema versionado de mercado.
- [ ] Implementar ingestÃ£o com freshness, deduplicaÃ§Ã£o e ajustes necessÃ¡rios.
- [ ] Criar backtester evitando look-ahead, survivorship e leakage.
- [ ] Modelar taxas, slippage, liquidez e latÃªncia de forma realista.
- [ ] Implementar walk-forward e comparaÃ§Ã£o com benchmarks.
- [ ] Criar paper broker isolado, reconciliaÃ§Ã£o e limites de risco.
- [ ] Exibir drawdown, exposiÃ§Ã£o, turnover e estabilidade, alÃ©m de retorno.
- [ ] Manter live execution ausente ou tecnicamente desabilitada.

**PortÃ£o:** uma estratÃ©gia reproduzÃ­vel passa por backtest fora da amostra e paper trading com controles de risco; nenhuma ordem real pode ser enviada.

## Fase 8 â€” PortfÃ³lio e economia do sistema

Objetivo: transformar o trabalho em evidÃªncia clara de engenharia e decidir o que merece continuidade.

- [ ] Criar modo demo com dados seguros e reproduzÃ­veis.
- [ ] Documentar arquitetura, decisÃµes e trade-offs por mÃ³dulo.
- [ ] Produzir estudos de caso com screenshots, mÃ©tricas e limitaÃ§Ãµes honestas.
- [ ] Comparar custo, tempo economizado, receita e manutenÃ§Ã£o por automaÃ§Ã£o.
- [ ] Pausar ou remover experimentos sem valor comprovado.
- [ ] Definir a prÃ³xima expansÃ£o somente a partir dos dados coletados.

**PortÃ£o:** outra pessoa entende, instala e demonstra o sistema; o proprietÃ¡rio consegue decidir objetivamente quais mÃ³dulos manter, melhorar ou abandonar.

## Backlog adiado deliberadamente

- acesso remoto e deployment pÃºblico;
- aplicativo mobile;
- multiusuÃ¡rio e permissÃµes complexas;
- Kubernetes e microserviÃ§os;
- n8n como dependÃªncia obrigatÃ³ria;
- autoposting sem aprovaÃ§Ã£o;
- outreach em massa;
- trading com dinheiro real;
- mÃºltiplos mÃ³dulos novos em paralelo.

Esses itens sÃ³ entram no roadmap por decisÃ£o explÃ­cita apÃ³s um checkpoint, nunca por conveniÃªncia durante outra tarefa.
