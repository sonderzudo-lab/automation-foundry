# Roadmap do Automation Foundry

Este roadmap ordena trabalho por dependência e evidência, não por prazo. Uma fase termina apenas quando seu portão é verificável.

## Legenda

- `[x]` concluído na branch de trabalho.
- `[ ]` pendente.
- `bloqueado local` exige o computador de casa, GPU, credenciais ou revisão manual.

## Princípios de prioridade

1. Primeiro tornar uma execução segura e observável.
2. Depois concluir um módulo que produza valor ponta a ponta.
3. Só então adicionar novos domínios.
4. Automatizar ações externas apenas após o fluxo manual ser confiável.
5. Medir custo, qualidade e valor antes de falar em escala.

## Fase 0 — Fundamentos do projeto

Objetivo: alinhar nome, regras e direção sem alterar prematuramente a implementação testada.

- [x] Renomear o repositório para `automation-foundry`.
- [x] Criar branch de estruturação.
- [x] Criar skills gerais e de domínio para Codex/Cursor e Claude Code.
- [x] Desabilitar a atribuição automática do Claude no projeto.
- [x] Definir arquitetura local-first e roadmap modular.
- [x] Revisar e fazer merge da branch após inspeção do usuário.

**Portão:** `AGENTS.md`, `ARQUITETURA.md`, `ROADMAP.md` e skills contam a mesma história; `main` permanece inalterada até aprovação.

## Fase 1 — Baseline e identidade técnica

Objetivo: fazer a base atual representar Automation Foundry e obter um baseline reproduzível.

- [x] Renomear package metadata, comando CLI, banco default, container e variáveis que ainda usam `content-engine`.
- [x] Catalogar arquivos vazios, stubs e implementação real.
- [x] Executar testes existentes e registrar o baseline.
- [x] Corrigir apenas falhas que bloqueiem o baseline, sem reescrever A1.
- [x] Atualizar `.env.example` para defaults locais seguros e loopback.
- [x] Definir comandos únicos para setup, lint, testes e execução. A execução da Fase 1 usa `automation-foundry doctor`; o dashboard permanece na Fase 3.
- [ ] Confirmar versões e specs no computador de casa. `bloqueado local`

**Portão:** um checkout limpo instala as dependências de desenvolvimento, executa os testes existentes e identifica honestamente o que está ou não implementado.

## Fase 2 — Kernel da plataforma

Objetivo: executar uma automação mínima usando contratos genéricos e estado durável.

- [x] Introduzir os conceitos `Automation`, `Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, `MetricPoint`, `Experiment`, `LedgerEntry` e `Alert`.
- [x] Criar migrations e separar tabelas compartilhadas das tabelas do Content Engine. Dezenove migrations incrementais cobrem o kernel compartilhado, o dispatch durável, continuacoes aprovadas e o baseline legado; `platform_alerts` e `alerts` permanecem domínios distintos.
- [x] Adotar PostgreSQL para o caminho concorrente; manter SQLite nos testes. O runtime local usa `asyncpg`, pool limitado e Compose preso a loopback; migrations, sessões concorrentes e replay idempotente foram validados em PostgreSQL 17 descartável.
- [x] Configurar Celery + Redis e filas `gpu`, `cpu`, `io`. A topologia fail-closed, os três workers seriais e o roteamento foram validados contra Redis real; probes efêmeros e o task de dispatch durável estão registrados.
- [x] Implementar dispatch durável de automações. A run e sua identidade estável são persistidas antes da publicação; falhas de broker permanecem reenviáveis, claims usam lease no PostgreSQL e entregas duplicadas não repetem o step. O fluxo foi validado com PostgreSQL, Redis e worker IO reais.
- [x] Implementar task wrapper idempotente com retries limitados, timeout, backoff exponencial e transições persistidas em processo único.
- [x] Implementar pedidos de cancelamento e kill switch persistentes, auditáveis e consultados pelo wrapper em processo único.
- [x] Registrar uma automação de exemplo e executar um step manual observável em processo único, sem efeito externo.
- [x] Registrar metric points decimais, idempotentes e atribuíveis, sem coleta ou efeito externo.
- [x] Registrar ledger entries decimais, append-only, idempotentes e atribuíveis, sem pagamentos ou ações financeiras.
- [x] Registrar alertas locais deduplicados, auditados e sem notificações externas.

**Portão:** uma run manual percorre `queued → running → succeeded/failed`, sobrevive a retry e deixa evidência completa no banco.

## Fase 3 — Control plane local

Objetivo: controlar a plataforma sem depender do terminal para a operação normal.

- [x] Criar FastAPI ligado a loopback por configuração validada, com `127.0.0.1` como default.
- [ ] Construir dashboard Jinja/HTMX para modulos, runs, steps e alertas. A visao read-only mostra automacoes, runs recentes, alertas ativos, schedules ordenados e indicadores por automacao sobre ate 50 resultados succeeded/failed recentes; runs canceladas ou abertas nao entram na taxa de sucesso, e a duracao media usa apenas timestamps validos. O detalhe de run mostra status, trigger, duracao, steps/tentativas/filas e evidencias vinculadas com redaction; status e steps usam um fragmento HTMX `no-store`, servido localmente, que consulta apenas runs nao terminais e degrada para a pagina completa sem JavaScript. Interacoes HTMX nas demais visoes, connectors, freshness e qualidade de dados permanecem pendentes.
- [ ] Adicionar fila de aprovacoes. A pagina inicial liga pendencias redigidas ao detalhe da run e mostra sua idade calculada no servidor; o detalhe permite rejeicao imutavel e aprovacao positiva com actor, motivo, confirmacao e protecao CSRF. Aprovacao positiva so aparece para uma projecao limitada e validada do payload; registros legados sem esse contexto permanecem bloqueados. O A1 possui revisao integral `no-store` do bundle validado por SHA-256 e so conclui depois da decisao; alteracao posterior do arquivo recusa a aprovacao e reverte a transacao. Resolver diretamente pela fila ainda permanece pendente.
- [ ] Permitir iniciar, cancelar, retentar, habilitar/desabilitar e acionar kill switch. O dashboard dispara executores registrados por um contrato fail-closed, cria retry como nova run ligada a uma falha retryable e permite cancelamento, habilitacao administrativa e kill switch com confirmacao, protecao CSRF e auditoria. `Automation.enabled` bloqueia novas runs e o inicio das enfileiradas sem interromper runs ativas; o kill switch permanece um controle operacional separado. O smoke inline e o Content Engine A1 em background estao registrados; os demais fluxos de dominio ainda precisam aderir ao contrato.
- [x] Adicionar schedules persistidos, desabilitados por padrão e com mudanças auditadas.
- [x] Integrar os schedules ao Celery Beat singleton e validar dispatch idempotente. O tick no worker IO usa ocorrências únicas no PostgreSQL, aplica misfire/sobreposição e reutiliza o dispatch durável; o fluxo real PostgreSQL + Redis + worker passou. O entrypoint Beat combina PID file com named mutex no Windows, cuja segunda aquisição foi recusada em teste.
- [x] Mostrar saúde de CPU, RAM, GPU/VRAM, disco, banco, Redis, workers e Beat em snapshot redigido. O dashboard não revela endpoints ou nomes de workers; integração com três workers reais e o probe local da RTX 3090 passaram.
- [ ] Mostrar custos e resultados atribuiveis. Totais exatos do ledger aparecem globalmente e por automacao e moeda, sem conversao, e observacoes vinculadas aparecem no detalhe da run. Analise de resultado e indicadores derivados permanecem pendentes.

**Portão:** pelo dashboard, o usuário dispara e acompanha uma run, resolve uma aprovação, diagnostica uma falha e desabilita o módulo.

## Fase 4 — Content Engine como primeiro módulo completo

Objetivo: transformar a implementação existente em uma automação editorial segura e demonstrável.

- [x] Integrar A1 ao contrato de runs sem perder seus testes atuais. O dashboard valida pauta, persona, formato e aberturas recentes, prepara dispatch duravel na fila `gpu` e acompanha run/step. O executor usa a geracao existente sem altera-la, grava bundle JSON atomico e revisavel, registra artifact com retencao, metrica e custo externo zero e aguarda approval editorial ligada ao hash do arquivo. A revisao integral omite o contexto privado do prompt; aprovacao conclui a run de forma idempotente, rejeicao cancela e bundle alterado bloqueia e reverte a decisao. Idempotencia, falha retryable, falha parcial de evidencia, retry explicito, cancelamento e entrega duplicada possuem testes. Ollama e GPU reais continuam `bloqueado local`.
- [ ] Implementar TTS comercialmente compativel. O contrato A2 ja consome somente bundle A1 aprovado, roda como step `gpu` ordinal 2, valida e registra WAV atomico, duracao, energia estimada, proveniencia/licenca e custo externo zero; retry, falha de audio, falha parcial de evidencia, continuation dispatch e entrega duplicada usam adapter fake. A auditoria separou engine/pesos Apache-2.0, eSpeak NG externo GPL-3.0-or-later e vozes PT-BR sem proveniencia individual documentada. O adapter `kokoro_quality_test` fixa pacote 0.9.4, revisao, peso e voz, exige CUDA/eSpeak, limita chunks e persiste o escopo nao monetizado; `disabled` permanece o default. Escuta humana das tres vozes na RTX 3090 e decisao explicita sobre o risco residual continuam `bloqueado local`.
- [ ] Implementar visuais, legendas e montagem em steps separados. A3 verifica bundle/audio, exige proveniencia/licenca comercial por asset e registra imagens + manifesto. Além dos fakes, `local_assets_quality_test` permite importar PNGs escolhidos pelo operador em raiz contida, com quantidade exata, SHA-256 e direitos explícitos por item, na fila `io`; o default segue `disabled`, não há download, FLUX.1 continua bloqueado e o manifesto não substitui revisão humana. A4 verifica WAV/narração, timestamps, idioma, similaridade e direitos comerciais e publica ASS; `approved_text_timing_quality_test` roda em `cpu` e estima intervalos determinísticos a partir do texto aprovado e da duração real, sem modelo, mas não substitui alinhamento ou revisão humana. A5 reconcilia todos os artifacts, roda na fila `cpu`, valida estrutura/tracks/resolução/duração do MP4 e registra export local, métricas, energia e custo. Além dos fakes, `ffmpeg_quality_test` fixa os hashes do build externo Gyan 8.0.1 GPLv3, usa subprocess sem shell e ffprobe; um short sintético H.264/AAC reproduzível passou com legenda visualmente confirmada. Os defaults continuam `disabled`; narração real, pacote visual real revisado, sincronismo revisado, long e política de instalação/redistribuição permanecem pendentes.
- [ ] Implementar similaridade e bloqueio editorial. O contrato A6 local já verifica roteiro/MP4, compara lexicalmente contra até 20 runs anteriores aprovadas no mesmo escopo de automação, registra relatório e métricas e bloqueia com alerta quando o score máximo atinge 0,85. Duplicata, conteúdo diferente, adulteração histórica, replay terminal e falha parcial possuem testes. Escopo explícito por canal, detecção semântica de paráfrases e um eventual override humano dedicado permanecem pendentes; o bloqueio atual é estrito e não publica nada.
- [ ] Exigir aprovação de pauta, ângulo, thumbnail e publicação.
- [ ] Começar com export local; depois upload privado por API oficial.
- [ ] Coletar analytics, custo e resultado por conteúdo.
- [ ] Validar modelos, FFmpeg, Ollama e consumo de VRAM. `bloqueado local`

**Portão:** uma pauta manual gera um pacote de vídeo revisável; nada é publicado sem aprovação; falhas e custos aparecem no dashboard.

## Fase 5 — Operação doméstica confiável

Objetivo: deixar o sistema rodar no computador principal com recuperação previsível.

- [x] Definir startup e shutdown seguros no Windows. O supervisor singleton executa preflight e migrations, inicia a topologia fixa em processos ocultos, usa shutdown cooperativo por eventos nomeados e Job Object como fallback, protege o status contra reutilizacao de PID e para somente os servicos Compose que iniciou, sem remover volumes. O ciclo completo isolado passou em Windows real; a ativacao com o volume PostgreSQL local depende de configurar o `.env`.
- [x] Implementar health checks locais redigidos e com timeout limitado.
- [x] Transformar degradações persistentes de health em alertas locais deduplicados. Duas observações degradadas abrem ou atualizam o alerta e duas saudáveis o resolvem; `skip` não altera o tratamento. Estado e eventos ficam no PostgreSQL, a task roda na fila `io` e não envia notificações externas.
- [x] Criar backup e restore de banco, configuração e artefatos. O bundle versionado publica snapshot, configuração allowlisted, artefatos e manifesto SHA-256 atomicamente. Restore exige confirmação exata, runtime parado, storage vazio e backup automático prévio; SQLite usa substituição atômica e PostgreSQL usa transação única. Um ciclo real passou em PostgreSQL 17 e volume Compose descartáveis; o volume persistente doméstico continua `bloqueado local`.
- [ ] Definir retenção e limpeza de storage.
- [ ] Testar reinício durante task, perda de Redis, falta de disco e indisponibilidade do Ollama.
- [ ] Documentar operação e troubleshooting reais. `bloqueado local`

**Portão:** após reinício ou falha simulada, o estado permanece consistente, não há execução duplicada e existe recuperação documentada.

## Fase 6 — Segundo módulo orientado a valor

Objetivo: provar que Automation Foundry é plataforma, não apenas um pipeline de vídeo.

Avaliar candidatos por valor esperado, qualidade de dados, risco, esforço e manutenção:

1. inteligência de mercado e concorrentes;
2. monitor de preços, promoções ou oportunidades;
3. pesquisa e qualificação de leads com rascunhos aprováveis;
4. geração recorrente de relatórios e briefs.

- [ ] Selecionar um único candidato com hipótese e métrica de sucesso.
- [ ] Especificar pelo contrato `$build-automation-module`.
- [ ] Implementar primeiro uma execução manual.
- [ ] Integrar estado, aprovações, custos e métricas ao dashboard.
- [ ] Avaliar o experimento antes de automatizar o schedule.

**Portão:** o segundo módulo reutiliza o kernel sem copiar o control plane e demonstra valor mensurável em um caso real.

## Fase 7 — Trading Research Lab

Objetivo: construir um projeto quantitativo sério para pesquisa e portfólio, sem dinheiro real.

- [ ] Definir fontes, universos, periodicidade e schema versionado de mercado.
- [ ] Implementar ingestão com freshness, deduplicação e ajustes necessários.
- [ ] Criar backtester evitando look-ahead, survivorship e leakage.
- [ ] Modelar taxas, slippage, liquidez e latência de forma realista.
- [ ] Implementar walk-forward e comparação com benchmarks.
- [ ] Criar paper broker isolado, reconciliação e limites de risco.
- [ ] Exibir drawdown, exposição, turnover e estabilidade, além de retorno.
- [ ] Manter live execution ausente ou tecnicamente desabilitada.

**Portão:** uma estratégia reproduzível passa por backtest fora da amostra e paper trading com controles de risco; nenhuma ordem real pode ser enviada.

## Fase 8 — Portfólio e economia do sistema

Objetivo: transformar o trabalho em evidência clara de engenharia e decidir o que merece continuidade.

- [ ] Criar modo demo com dados seguros e reproduzíveis.
- [ ] Documentar arquitetura, decisões e trade-offs por módulo.
- [ ] Produzir estudos de caso com screenshots, métricas e limitações honestas.
- [ ] Comparar custo, tempo economizado, receita e manutenção por automação.
- [ ] Pausar ou remover experimentos sem valor comprovado.
- [ ] Definir a próxima expansão somente a partir dos dados coletados.

**Portão:** outra pessoa entende, instala e demonstra o sistema; o proprietário consegue decidir objetivamente quais módulos manter, melhorar ou abandonar.

## Backlog adiado deliberadamente

- acesso remoto e deployment público;
- aplicativo mobile;
- multiusuário e permissões complexas;
- Kubernetes e microserviços;
- n8n como dependência obrigatória;
- autoposting sem aprovação;
- outreach em massa;
- trading com dinheiro real;
- múltiplos módulos novos em paralelo.

Esses itens só entram no roadmap por decisão explícita após um checkpoint, nunca por conveniência durante outra tarefa.
