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

- [ ] Introduzir os conceitos `Automation`, `Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, `MetricPoint`, `Experiment`, `LedgerEntry` e `Alert`. `Automation`, `Run`, `StepRun` e `Approval` estÃ£o implementados; os demais permanecem pendentes.
- [ ] Criar migrations e separar tabelas compartilhadas das tabelas do Content Engine. Quatro migrations incrementais cobrem o kernel compartilhado; o baseline legado permanece pendente.
- [ ] Adotar PostgreSQL para o caminho concorrente; manter SQLite nos testes.
- [ ] Configurar Celery + Redis e filas `gpu`, `cpu`, `io`.
- [x] Implementar task wrapper idempotente com retries limitados, timeout, backoff exponencial e transiÃ§Ãµes persistidas em processo Ãºnico.
- [x] Implementar pedidos de cancelamento e kill switch persistentes, auditÃ¡veis e consultados pelo wrapper em processo Ãºnico.
- [x] Registrar uma automaÃ§Ã£o de exemplo e executar um step manual observÃ¡vel em processo Ãºnico, sem efeito externo.

**PortÃ£o:** uma run manual percorre `queued â†’ running â†’ succeeded/failed`, sobrevive a retry e deixa evidÃªncia completa no banco.

## Fase 3 â€” Control plane local

Objetivo: controlar a plataforma sem depender do terminal para a operaÃ§Ã£o normal.

- [ ] Criar FastAPI ligado a `127.0.0.1`.
- [ ] Construir dashboard Jinja/HTMX para mÃ³dulos, runs, steps e alertas.
- [ ] Adicionar fila de aprovaÃ§Ãµes.
- [ ] Permitir iniciar, cancelar, retentar e acionar kill switch.
- [ ] Adicionar schedules persistidos e Celery Beat singleton.
- [ ] Mostrar saÃºde de CPU, RAM, GPU/VRAM, disco, banco, Redis e workers.
- [ ] Mostrar custos e resultados atribuÃ­veis.

**PortÃ£o:** pelo dashboard, o usuÃ¡rio dispara e acompanha uma run, resolve uma aprovaÃ§Ã£o, diagnostica uma falha e desabilita o mÃ³dulo.

## Fase 4 â€” Content Engine como primeiro mÃ³dulo completo

Objetivo: transformar a implementaÃ§Ã£o existente em uma automaÃ§Ã£o editorial segura e demonstrÃ¡vel.

- [ ] Integrar A1 ao contrato de runs sem perder seus testes atuais.
- [ ] Implementar TTS comercialmente compatÃ­vel.
- [ ] Implementar visuais, legendas e montagem em steps separados.
- [ ] Implementar similaridade e bloqueio editorial.
- [ ] Exigir aprovaÃ§Ã£o de pauta, Ã¢ngulo, thumbnail e publicaÃ§Ã£o.
- [ ] ComeÃ§ar com export local; depois upload privado por API oficial.
- [ ] Coletar analytics, custo e resultado por conteÃºdo.
- [ ] Validar modelos, FFmpeg, Ollama e consumo de VRAM. `bloqueado local`

**PortÃ£o:** uma pauta manual gera um pacote de vÃ­deo revisÃ¡vel; nada Ã© publicado sem aprovaÃ§Ã£o; falhas e custos aparecem no dashboard.

## Fase 5 â€” OperaÃ§Ã£o domÃ©stica confiÃ¡vel

Objetivo: deixar o sistema rodar no computador principal com recuperaÃ§Ã£o previsÃ­vel.

- [ ] Definir startup e shutdown seguros no Windows.
- [ ] Implementar health checks e alertas locais.
- [ ] Criar backup e restore de banco, configuraÃ§Ã£o e artefatos.
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
