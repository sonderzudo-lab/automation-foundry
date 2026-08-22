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
- [x] Criar migrations e separar tabelas compartilhadas das tabelas do Content Engine. Vinte e duas migrations incrementais cobrem o kernel compartilhado, o dispatch durável, continuacoes aprovadas, a evidencia de expurgo de artifacts, escolhas estruturadas de approval, observações redigidas de connectors e o baseline legado; `platform_alerts` e `alerts` permanecem domínios distintos.
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
- [x] Construir dashboard Jinja/HTMX para modulos, runs, steps e alertas. A visão `no-store` mostra automações, runs recentes, alertas ativos, schedules ordenados e indicadores por automação sobre até 50 resultados succeeded/failed recentes; runs canceladas ou abertas não entram na taxa de sucesso, e a duração média usa apenas timestamps válidos. O detalhe de run mostra status, trigger, duração, steps/tentativas/filas e evidências vinculadas com redaction; status e steps usam fragmento HTMX `no-store` somente enquanto a run não é terminal. O contrato append-only `ConnectorObservation` persiste status redigido, SLO da observação, último sucesso, SLO de freshness e qualidade normalizada sem endpoint, credencial ou erro bruto; timestamps futuros são recusados e o score decimal permanece exato também no schema SQLite criado por Alembic. A home exibe somente a observação mais recente por connector, prioriza estados indisponíveis/degradados, sinaliza truncamento, mostra UTC absoluto e calcula `fresh`/`stale` no servidor; o bloco atualiza por outro fragmento HTMX `no-store`, e ambas as páginas degradam para HTML completo sem JavaScript. O histórico ainda não possui política de retenção.
- [x] Adicionar fila de aprovacoes. A pagina inicial mostra a idade calculada no servidor e liga diretamente as pendencias A1, A2, A7 e A8 às páginas `no-store` de evidência integral. A1 reconfere o bundle por SHA-256; A2 transmite o WAV verificado e informa o plano visual exato; A7 transmite o MP4 verificado com evidência de originalidade; A8 serve somente os PNGs A3 do conjunto congelado. Nessas páginas, o operador aprova ou rejeita com actor, motivo, confirmação e proteção CSRF; A8 exige ainda a escolha exata validada no servidor. A decisão é imutável e, depois dela, a página permanece somente leitura. Approvals genéricas continuam usando a projeção limitada e validada do detalhe da run; registros legados sem contexto seguro permanecem bloqueados.
- [x] Permitir iniciar, cancelar, retentar, habilitar/desabilitar e acionar kill switch. O dashboard dispara executores registrados por um contrato fail-closed, cria retry como nova run ligada a uma falha retryable e permite cancelamento, habilitacao administrativa, controle de schedule e kill switch com confirmacao, protecao CSRF e auditoria. `Automation.enabled` bloqueia novas runs e o inicio das enfileiradas sem interromper runs ativas; habilitação administrativa e kill switch usam lock de linha para não duplicar eventos concorrentes, mantendo controles separados. Habilitar um schedule reconfirma no servidor que a automacao esta ativa, sem kill switch e com executor registrado, calcula a proxima ocorrencia e registra evento append-only sob lock de linha; duas requisições PostgreSQL concorrentes e replay sequencial produzem no máximo um evento. Estado efetivo e motivos allowlisted aparecem na UI, bloqueios esperados retornam ao dashboard e desabilitar permanece possível sob kill switch. O smoke inline e o Content Engine A1 em background estao registrados; futuros fluxos de dominio devem aderir ao mesmo contrato.
- [x] Adicionar schedules persistidos, desabilitados por padrÃ£o e com mudanÃ§as auditadas.
- [x] Integrar os schedules ao Celery Beat singleton e validar dispatch idempotente. O tick no worker IO usa ocorrÃªncias Ãºnicas no PostgreSQL, aplica misfire/sobreposiÃ§Ã£o e reutiliza o dispatch durÃ¡vel; o fluxo real PostgreSQL + Redis + worker passou. O entrypoint Beat combina PID file com named mutex no Windows, cuja segunda aquisiÃ§Ã£o foi recusada em teste.
- [x] Mostrar saÃºde de CPU, RAM, GPU/VRAM, disco, banco, Redis, workers e Beat em snapshot redigido. O dashboard nÃ£o revela endpoints ou nomes de workers; integraÃ§Ã£o com trÃªs workers reais e o probe local da RTX 3090 passaram.
- [x] Mostrar custos e resultados atribuiveis. O dashboard soma valores decimais exatos do ledger globalmente e por automação e moeda, separa custo, receita e valor atribuído e calcula a receita líquida observada como receita menos custo. Observações vinculadas aparecem no detalhe da run. Não há conversão cambial, previsão, pagamento ou promessa; análise estatística e rentabilidade por experimento permanecem para fases posteriores.

**PortÃ£o:** pelo dashboard, o usuÃ¡rio dispara e acompanha uma run, resolve uma aprovaÃ§Ã£o, diagnostica uma falha e desabilita o mÃ³dulo.

## Fase 4 â€” Content Engine como primeiro mÃ³dulo completo

Objetivo: transformar a implementaÃ§Ã£o existente em uma automaÃ§Ã£o editorial segura e demonstrÃ¡vel.

- [x] Integrar A1 ao contrato de runs sem perder seus testes atuais. O dashboard valida pauta, persona, formato e aberturas recentes, prepara dispatch duravel na fila `gpu` e acompanha run/step. O executor usa a geracao existente sem altera-la, grava bundle JSON atomico e revisavel, registra artifact com retencao, metrica e custo externo zero e aguarda approval editorial ligada ao hash do arquivo. A revisao integral omite o contexto privado do prompt; aprovacao conclui a run de forma idempotente, rejeicao cancela e bundle alterado bloqueia e reverte a decisao. Idempotencia, falha retryable, falha parcial de evidencia, retry explicito, cancelamento e entrega duplicada possuem testes. Ollama e GPU reais continuam `bloqueado local`.
- [ ] Implementar TTS comercialmente compativel. O contrato A2 ja consome somente bundle A1 aprovado, roda como step `gpu` ordinal 2, valida e registra WAV atomico, duracao, energia estimada, proveniencia/licenca e custo externo zero; retry, falha de audio, falha parcial de evidencia, continuation dispatch e entrega duplicada usam adapter fake. A auditoria separou engine/pesos Apache-2.0, eSpeak NG externo GPL-3.0-or-later e vozes PT-BR sem proveniencia individual documentada. O adapter `kokoro_quality_test` fixa pacote 0.9.4, revisao, peso e voz, exige CUDA/eSpeak, limita chunks e persiste o escopo nao monetizado; `disabled` permanece o default. Na RTX 3090, CUDA 12.8, PyTorch `2.7.1+cu128`, eSpeak NG e o teste Kokoro curto passaram com as tres vozes PT-BR permitidas; uma execução real com `pm_alex` produziu WAV de 25,85 s e recebeu aprovação auditiva do proprietário. O gate opcional `CONTENT_NARRATION_REVIEW_ENABLED` agora pausa A2 antes de A3, vincula a decisão ao WAV exato e informa a quantidade/dimensões dos PNGs necessários. A proveniência comercial das vozes continua pendente e o escopo não monetizado permanece obrigatório.
- [ ] Implementar visuais, legendas e montagem em steps separados. A3 verifica bundle/audio, exige proveniencia/licenca comercial por asset e registra imagens + manifesto. Além dos fakes, `local_assets_quality_test` permite importar PNGs escolhidos pelo operador em raiz contida, com quantidade exata, SHA-256 e direitos explícitos por item, na fila `io`; o default segue `disabled`, não há download, FLUX.1 continua bloqueado e o manifesto não substitui revisão humana. A4 verifica WAV/narração, timestamps, idioma, similaridade e direitos comerciais e publica ASS; `approved_text_timing_quality_test` roda em `cpu` e estima intervalos determinísticos a partir do texto aprovado e da duração real, sem modelo, mas não substitui alinhamento ou revisão humana. O diagnóstico auditável de alinhamento já existe em `src/pipeline/caption_alignment.py`: somente leitura, ele reconfere os dois artifacts por SHA-256, mede a energia do WAV em janelas de 20 ms, compara os trechos de fala com os eventos do ASS e registra relatório, métricas allowlisted e alerta local quando cobertura, silêncio coberto, desvio de início/fim ou eventos além do áudio saem da tolerância. O detalhe da run reexibe o último relatório depois de reconferir SHA-256 e validar schema, status e motivos, degradando para um aviso seguro quando a evidência não confere. Ele expõe exatamente a deriva que o timing proporcional não detecta, mas não corrige a legenda, não bloqueia a run e não substitui revisão humana. A5 reconcilia todos os artifacts, roda na fila `cpu`, valida estrutura/tracks/resolução/duração do MP4 e registra export local, métricas, energia e custo. Além dos fakes, `ffmpeg_quality_test` fixa os hashes do build externo Gyan 8.0.1 GPLv3, usa subprocess sem shell e ffprobe; um short sintético H.264/AAC reproduzível passou com legenda visualmente confirmada. Os defaults continuam `disabled`; narração real, pacote visual real revisado, sincronismo revisado, long e política de instalação/redistribuição permanecem pendentes. Um ensaio local A1→A6 já encadeia os backends reais de A3 e A4 na suíte padrão e, em modo opt-in com FFmpeg fixado, produziu um MP4 1080×1920 H.264/AAC de 12 s com legenda queimada e relatório A6, usando somente mídia sintética gerada pelo próprio projeto e declarada como quality test.
- [ ] Implementar similaridade e bloqueio editorial. O contrato A6 local já verifica roteiro/MP4, compara lexicalmente contra até 20 runs anteriores aprovadas no mesmo escopo de automação, registra relatório e métricas e bloqueia com alerta quando o score máximo atinge 0,85. Duplicata, conteúdo diferente, adulteração histórica, replay terminal e falha parcial possuem testes. O escopo explícito por canal foi investigado e não foi implementado: `channels` é uma tabela legada que nenhum código cria ou consulta, não existe vínculo persistido entre `Run` e canal, e a identidade de canal está atrelada a plataforma e credenciais de publicação, que estão fora do escopo atual. Inventar esse contrato agora significaria aceitar um identificador arbitrário sem validação durável no servidor, então ele fica registrado como decisão de produto pendente. Detecção semântica de paráfrases e um eventual override humano dedicado também permanecem pendentes; o bloqueio atual é estrito e não publica nada.
- [ ] Exigir aprovaÃ§Ã£o de pauta, Ã¢ngulo, thumbnail e publicaÃ§Ã£o. A1 cobre pauta, ângulo e roteiro; A7 revisa o vídeo final e sua originalidade; A8 cobre a thumbnail. Com `CONTENT_THUMBNAIL_REVIEW_ENABLED=true` junto de A7, a aprovação A7 abre uma nova approval sobre o conjunto exato de PNGs A3 da mesma run. A página `no-store` reconfere e serve cada candidato localmente, o formulário aceita exatamente uma escolha validada no servidor, e a decisão persiste ID + SHA-256 antes de concluir com `published=false`. Rejeição cancela; adulteração, ID fora do conjunto, digest/payload divergente e replay falham com segurança. Ambos os defaults seguem `false`; o gate específico de publicação permanece pendente e nenhum upload existe.
- [x] Começar com export local. Runs concluídas pelos gates A7/A8 oferecem no dashboard
  loopback um manifesto JSON canônico e downloads do MP4 e, quando selecionado, do PNG.
  Cada GET reconstrói as approvals, a decisão A8 e os SHA-256 antes de servir; adulteração
  ou omissão da evidência A8 existente responde 409, caminhos locais nunca aparecem e
  `published=false` permanece explícito. A verificação A7 é reutilizada dentro da mesma
  requisição para evitar um segundo hash integral do MP4.
- [ ] Adicionar upload privado por API oficial, com gate próprio e autorização explícita.
- [ ] Coletar analytics, custo e resultado por conteÃºdo. O detalhe da run já consolida, com
  precisão decimal e isolamento por run, os indicadores editoriais A1→A6 allowlisted, a energia
  estimada e o ledger local por moeda (custo, receita, valor atribuído e receita líquida
  observada). A projeção não expõe source, category ou dimensions e declara que nenhuma plataforma
  externa foi consultada. Analytics pós-publicação por API oficial, freshness do connector e
  atribuição de resultado real continuam pendentes; nenhuma publicação ou chamada externa foi
  adicionada.
- [ ] Validar modelos, FFmpeg, Ollama e consumo de VRAM. `bloqueado local`

**PortÃ£o:** uma pauta manual gera um pacote de vÃ­deo revisÃ¡vel; nada Ã© publicado sem aprovaÃ§Ã£o; falhas e custos aparecem no dashboard.

## Fase 5 â€” OperaÃ§Ã£o domÃ©stica confiÃ¡vel

Objetivo: deixar o sistema rodar no computador principal com recuperaÃ§Ã£o previsÃ­vel.

- [x] Definir startup e shutdown seguros no Windows. O supervisor singleton executa preflight e migrations, inicia a topologia fixa em processos ocultos, usa shutdown cooperativo por eventos nomeados e Job Object como fallback, protege o status contra reutilizacao de PID e para somente os servicos Compose que iniciou, sem remover volumes. O ciclo completo passou em Windows real com PostgreSQL persistente, Redis, dashboard, Beat e três workers; stop/start cooperativo em idle preservou os serviços Compose não pertencentes ao supervisor.
- [x] Implementar health checks locais redigidos e com timeout limitado. O probe real dos três workers no Windows mantém o timeout interno do Celery e uma margem externa limitada para o overhead do inspect; duas observações saudáveis resolveram automaticamente o alerta durável criado pelo falso timeout anterior.
- [x] Transformar degradaÃ§Ãµes persistentes de health em alertas locais deduplicados. Duas observaÃ§Ãµes degradadas abrem ou atualizam o alerta e duas saudÃ¡veis o resolvem; `skip` nÃ£o altera o tratamento. Estado e eventos ficam no PostgreSQL, a task roda na fila `io` e nÃ£o envia notificaÃ§Ãµes externas.
- [x] Criar backup e restore de banco, configuraÃ§Ã£o e artefatos. O bundle versionado publica snapshot, configuraÃ§Ã£o allowlisted, artefatos e manifesto SHA-256 atomicamente. Restore exige confirmaÃ§Ã£o exata, runtime parado, storage vazio e backup automÃ¡tico prÃ©vio; SQLite usa substituiÃ§Ã£o atÃ´mica e PostgreSQL usa transaÃ§Ã£o Ãºnica. Um ciclo real de backup/restore passou em PostgreSQL 17 e volume Compose descartÃ¡veis; o backup verificado também passou contra o volume persistente doméstico, enquanto o restore desse volume continua `bloqueado local`.
- [x] Definir retenção e limpeza de storage. O inventário somente leitura já existe:
      `src/operations/retention.py` percorre `STORAGE_ROOT` sem seguir links, reconcilia cada
      arquivo com os artifacts registrados e classifica em `retained`, `retention_hold`,
      `expired`, `orphan`, `missing` e `unsafe`, com contagem e bytes por classe e por automação.
      Artefatos de runs abertas, de runs com approval pendente, com tamanho divergente do registro
      ou com caminho compartilhado por mais de um registro nunca aparecem como expirados. Caminhos
      registrados que escapam da raiz e symlinks viram `unsafe` sem serem seguidos, e uma varredura
      maior que o limite configurado falha em vez de produzir resultado parcial.
      `automation-foundry storage-inventory [--json]` é dry-run por default e recusa
      `--no-dry-run` com código 2; a página `/storage` mostra somente totais redigidos.
      O expurgo material existe em `src/operations/purge.py`, desabilitado por default
      (`RETENTION_PURGE_ENABLED=false`) e dividido em dois atos deliberados. `plan-purge`
      transforma os itens `expired` em uma lista imutável verificada por SHA-256 e abre uma
      approval humana ligada ao digest exato dessa lista, sem apagar nada; a projeção revisada não
      contém caminho de arquivo. `execute-purge` exige a approval aprovada, o runtime parado, a
      confirmação `PURGE <plan-digest>`, um backup verificado criado imediatamente antes, e
      reconfere o SHA-256 de cada arquivo duas vezes: uma varredura completa que aborta tudo antes
      da primeira remoção e outra imediatamente antes de cada `unlink`. Artefatos removidos ficam
      marcados com `purged_at`/`purged_by_approval_id` e aparecem como `purged`, não como
      `missing`; um arquivo que reaparece no caminho purgado vira `unsafe`. Falha após a primeira
      remoção registra a evidência parcial, deixa a run `failed` e mantém o backup prévio como
      caminho de recuperação documentado. Rejeitar cancela a run e não apaga nada.
      A política declarativa de retenção por tipo de artefato existe em
      `src/operations/retention_policy.py`: uma tabela única e validada declara retenção, produtor
      e justificativa de cada tipo, os produtores A1→A6 e o diagnóstico de alinhamento resolvem
      dela, `register-artifact` a usa como default e um teste de guarda recusa qualquer tipo
      produzido em `src/` que não esteja declarado. Tipo não declarado fica com retenção
      indefinida, nunca expira e por isso nunca entra em um plano de expurgo por omissão; a
      política vale apenas para registros novos, porque o prazo é congelado na linha do artifact.
      `automation-foundry retention-policy` e a página `/storage` mostram a declaração.
      Ainda faltam: tratamento de órfãos no disco (sem registro nem hash conhecido) e expurgo
      agendado.
- [x] Testar reinÃ­cio durante task, perda de Redis, falta de disco e indisponibilidade do Ollama por simulaÃ§Ãµes locais. Lease expirada permite reclaim auditado, tentativa `running` abandonada vira falha estruturada e entrega duplicada terminal nÃ£o cria outro step. Perda de Redis entre sessÃµes deixa a mesma entrega `pending`, e a republicaÃ§Ã£o posterior preserva uma Ãºnica run/step. `ENOSPC` e conexÃ£o recusada pelo Ollama falham com erro redigido/retryable, sem artifact ou approval parcial. PostgreSQL persistente, Redis, Ollama e os três workers reais passaram no runtime doméstico, inclusive uma run durável sem efeito externo e um stop/start em idle; interrupção durante task, perda real do Redis nesse volume e ENOSPC em filesystem real continuam `bloqueado local`.
- [x] Documentar operaÃ§Ã£o e troubleshooting locais. `docs/operations/local-recovery.md`
  explica diagnÃ³stico e recuperaÃ§Ã£o reversÃ­vel de Redis, Ollama, reinÃ­cio e
  falta de espaÃ§o, sem recriar runs ou tocar em dados. O stop/start real em idle passou contra o
  volume PostgreSQL doméstico; reinício durante task e ENOSPC em filesystem real permanecem
  `bloqueado local`.

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
