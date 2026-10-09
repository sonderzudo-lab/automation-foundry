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
- [x] Criar migrations e separar tabelas compartilhadas das tabelas do Content Engine. Vinte e duas migrations incrementais cobrem o kernel compartilhado, o dispatch durável, continuacoes aprovadas, a evidencia de expurgo de artifacts, escolhas estruturadas de approval, observações redigidas de connectors e o baseline legado; `platform_alerts` e `alerts` permanecem domínios distintos.
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
- [x] Construir dashboard Jinja/HTMX para modulos, runs, steps e alertas. A visão `no-store` mostra automações, runs recentes, alertas ativos, schedules ordenados e indicadores por automação sobre até 50 resultados succeeded/failed recentes; runs canceladas ou abertas não entram na taxa de sucesso, e a duração média usa apenas timestamps válidos. O detalhe de run mostra status, trigger, duração, steps/tentativas/filas e evidências vinculadas com redaction; status e steps usam fragmento HTMX `no-store` somente enquanto a run não é terminal. O contrato append-only `ConnectorObservation` persiste status redigido, SLO da observação, último sucesso, SLO de freshness e qualidade normalizada sem endpoint, credencial ou erro bruto; timestamps futuros são recusados e o score decimal permanece exato também no schema SQLite criado por Alembic. A home exibe somente a observação mais recente por connector, prioriza estados indisponíveis/degradados, sinaliza truncamento, mostra UTC absoluto e calcula `fresh`/`stale` no servidor; o bloco atualiza por outro fragmento HTMX `no-store`, e ambas as páginas degradam para HTML completo sem JavaScript. O histórico ainda não possui política de retenção.
- [x] Adicionar fila de aprovacoes. A pagina inicial mostra a idade calculada no servidor e liga diretamente as pendencias A1, A2, A7 e A8 às páginas `no-store` de evidência integral. A1 reconfere o bundle por SHA-256; A2 transmite o WAV verificado e informa o plano visual exato; A7 transmite o MP4 verificado com evidência de originalidade; A8 serve somente os PNGs A3 do conjunto congelado. Nessas páginas, o operador aprova ou rejeita com actor, motivo, confirmação e proteção CSRF; A8 exige ainda a escolha exata validada no servidor. A decisão é imutável e, depois dela, a página permanece somente leitura. Approvals genéricas continuam usando a projeção limitada e validada do detalhe da run; registros legados sem contexto seguro permanecem bloqueados.
- [x] Permitir iniciar, cancelar, retentar, habilitar/desabilitar e acionar kill switch. O dashboard dispara executores registrados por um contrato fail-closed, cria retry como nova run ligada a uma falha retryable e permite cancelamento, habilitacao administrativa, controle de schedule e kill switch com confirmacao, protecao CSRF e auditoria. `Automation.enabled` bloqueia novas runs e o inicio das enfileiradas sem interromper runs ativas; habilitação administrativa e kill switch usam lock de linha para não duplicar eventos concorrentes, mantendo controles separados. Habilitar um schedule reconfirma no servidor que a automacao esta ativa, sem kill switch e com executor registrado, calcula a proxima ocorrencia e registra evento append-only sob lock de linha; duas requisições PostgreSQL concorrentes e replay sequencial produzem no máximo um evento. Estado efetivo e motivos allowlisted aparecem na UI, bloqueios esperados retornam ao dashboard e desabilitar permanece possível sob kill switch. O smoke inline e o Content Engine A1 em background estao registrados; futuros fluxos de dominio devem aderir ao mesmo contrato.
- [x] Adicionar schedules persistidos, desabilitados por padrão e com mudanças auditadas.
- [x] Integrar os schedules ao Celery Beat singleton e validar dispatch idempotente. O tick no worker IO usa ocorrências únicas no PostgreSQL, aplica misfire/sobreposição e reutiliza o dispatch durável; o fluxo real PostgreSQL + Redis + worker passou. O entrypoint Beat combina PID file com named mutex no Windows, cuja segunda aquisição foi recusada em teste.
- [x] Mostrar saúde de CPU, RAM, GPU/VRAM, disco, banco, Redis, workers e Beat em snapshot redigido. O dashboard não revela endpoints ou nomes de workers; integração com três workers reais e o probe local da RTX 3090 passaram.
- [x] Mostrar custos e resultados atribuiveis. O dashboard soma valores decimais exatos do ledger globalmente e por automação e moeda, separa custo, receita e valor atribuído e calcula a receita líquida observada como receita menos custo. Observações vinculadas aparecem no detalhe da run. Não há conversão cambial, previsão, pagamento ou promessa; análise estatística e rentabilidade por experimento permanecem para fases posteriores.
- [x] Redesenhar a camada visual do dashboard sem alterar contratos de dados nem rotas. `base.html`, macros e um `styles.css` único com tema claro/escuro substituem o CSS embutido; Visão geral, detalhe da run, as cinco revisões de approval, saúde, storage e export local usam o mesmo shell, selos de estado com ícone e texto, diálogos e abas sem JavaScript. A home prioriza o que exige ação ("Precisa de você") e recolhe controles administrativos. Recusas de formulário (400, 403, 404, 409, 413, 415, 422, 503) e rotas inexistentes aparecem como página HTML com a causa em português e caminho de volta quando o cliente aceita `text/html`; mídia, HTMX e clientes de API continuam recebendo JSON. Verificado por testes de renderização e por inspeção visual em Chromium, incluindo, em servidor de demonstração isolado (SQLite temporário, publisher de fila substituído), disparo de run, cancelamento e polling de 2 s pelo navegador; a navegação por teclado também foi percorrida no navegador (skip link, ordem de Tab, menus, diálogos com foco contido e devolvido a quem os abriu, gaveta do menu abaixo de 56 rem, abas por setas e fragmentos que não roubam o foco). As cinco páginas de revisão (A1, A2, A7, A8 e brief) e as mutações pela UI (aprovar, rejeitar, escolher thumbnail, decidir approval sem página própria, kill switch, desabilitar e habilitar módulo, habilitar schedule, cancelar e retentar run) foram exercitadas no navegador contra uma demo isolada em que um mini-worker local executa os dispatches; a página da run recarrega uma vez quando a run termina para que as ações do cabeçalho reflitam o estado final, e a home não oferece disparo manual com kill switch ativo. Leitor de tela, Firefox e Safari ainda não foram verificados; áudio e vídeo foram conferidos só por tipo e status de resposta, não por reprodução.

**Portão:** pelo dashboard, o usuário dispara e acompanha uma run, resolve uma aprovação, diagnostica uma falha e desabilita o módulo.

## Fase 4 — Content Engine como primeiro módulo completo

Objetivo: transformar a implementação existente em uma automação editorial segura e demonstrável.

- [x] Integrar A1 ao contrato de runs sem perder seus testes atuais. O dashboard valida pauta, persona, formato e aberturas recentes, prepara dispatch duravel na fila `gpu` e acompanha run/step. O executor usa a geracao existente sem altera-la, grava bundle JSON atomico e revisavel, registra artifact com retencao, metrica e custo externo zero e aguarda approval editorial ligada ao hash do arquivo. A revisao integral omite o contexto privado do prompt; aprovacao conclui a run de forma idempotente, rejeicao cancela e bundle alterado bloqueia e reverte a decisao. Idempotencia, falha retryable, falha parcial de evidencia, retry explicito, cancelamento e entrega duplicada possuem testes. A geração local também passou na run 4 como início da cadeia real A1→A8.
- [ ] Implementar TTS comercialmente compativel. O contrato A2 ja consome somente bundle A1 aprovado, roda como step `gpu` ordinal 2, valida e registra WAV atomico, duracao, energia estimada, proveniencia/licenca e custo externo zero; retry, falha de audio, falha parcial de evidencia, continuation dispatch e entrega duplicada usam adapter fake. A auditoria separou engine/pesos Apache-2.0, eSpeak NG externo GPL-3.0-or-later e vozes PT-BR sem proveniencia individual documentada. O adapter `kokoro_quality_test` fixa pacote 0.9.4, revisao, peso e voz, exige CUDA/eSpeak, limita chunks e persiste o escopo nao monetizado; `disabled` permanece o default. Na RTX 3090, CUDA 12.8, PyTorch `2.7.1+cu128`, eSpeak NG e o teste Kokoro curto passaram com as tres vozes PT-BR permitidas; `pm_alex` produziu WAVs integrais de 25,85 s e 39,325 s. O segundo passou pelo gate opcional `CONTENT_NARRATION_REVIEW_ENABLED`, que vinculou a aprovação do proprietário ao WAV exato antes de liberar os sete PNGs 1080×1920 exigidos por A3. A proveniência comercial das vozes continua pendente e o escopo não monetizado permanece obrigatório.
- [ ] Implementar visuais, legendas e montagem em steps separados. A3 verifica bundle/audio, exige proveniencia/licenca comercial por asset e registra imagens + manifesto. Além dos fakes, `local_assets_quality_test` permite importar PNGs escolhidos pelo operador em raiz contida, com quantidade exata, SHA-256 e direitos explícitos por item, na fila `io`; o default segue `disabled`, não há download, FLUX.1 continua bloqueado e o manifesto não substitui revisão humana. A4 verifica WAV/narração, timestamps, idioma, similaridade e direitos comerciais e publica ASS. `approved_text_timing_quality_test` permanece como fallback determinístico em `cpu`. O adapter `faster_whisper_small_quality_test` adiciona timestamps por palavra guiados pelo áudio na fila `gpu`, com pacote/runtime fixados, modelo MIT `Systran/faster-whisper-small` em revisão fixa, pesos conferidos por SHA-256 e snapshot exclusivamente local; ele não baixa no worker e não é descrito como alinhamento forçado clássico. O teste opt-in com uma narração `pm_alex` fresca passou na RTX 3090, inclusive o reparo limitado de um token com duração zero emitido pelo modelo, e produziu ASS válido. O diagnóstico auditável em `src/pipeline/caption_alignment.py` reconfere WAV/ASS por SHA-256, mede energia em janelas de 20 ms e registra relatório, métricas e alerta. `CONTENT_CAPTION_ALIGNMENT_GATE_ENABLED`, desabilitado por default, exige A4/A5/A7, persiste um step `cpu` ligado aos artifacts exatos e só abre A7 em `pass`; `warning` ou evidência inválida falham fechado sem publicação. O relatório aprovado entra no digest e na página de A7 e é preservado por A8. A5 reconcilia todos os artifacts, roda na fila `cpu`, valida estrutura/tracks/resolução/duração do MP4 e registra export local, métricas, energia e custo. Além dos fakes, `ffmpeg_quality_test` fixa os hashes do build externo Gyan 8.0.1 GPLv3, usa subprocess sem shell e ffprobe. Além do ensaio sintético reproduzível, a run 4 opt-in atravessou A1→A8 com narração `pm_alex`, sete PNGs 1080×1920, ASS, FFmpeg real e revisão humana, produzindo MP4 H.264/AAC de 39,325 s; o diagnóstico de alinhamento retornou `pass` e o export reconferido permaneceu `published=false`. Os defaults continuam `disabled`; o novo adapter ainda precisa atravessar uma run real A1→A8. Formato long, correção/alinhamento forçado clássico, revisão jurídica independente dos assets e política de instalação/redistribuição permanecem pendentes.
- [ ] Implementar similaridade e bloqueio editorial. O contrato A6 local já verifica roteiro/MP4, compara lexicalmente contra até 20 runs anteriores aprovadas no mesmo escopo de automação, registra relatório e métricas e bloqueia com alerta quando o score máximo atinge 0,85. Duplicata, conteúdo diferente, adulteração histórica, replay terminal e falha parcial possuem testes. O escopo explícito por canal foi investigado e não foi implementado: `channels` é uma tabela legada que nenhum código cria ou consulta, não existe vínculo persistido entre `Run` e canal, e a identidade de canal está atrelada a plataforma e credenciais de publicação, que estão fora do escopo atual. Inventar esse contrato agora significaria aceitar um identificador arbitrário sem validação durável no servidor, então ele fica registrado como decisão de produto pendente. Detecção semântica de paráfrases e um eventual override humano dedicado também permanecem pendentes; o bloqueio atual é estrito e não publica nada.
- [ ] Exigir aprovação de pauta, ângulo, thumbnail e publicação. A1 cobre pauta, ângulo e roteiro; A2 permite aprovar a narração exata antes dos visuais; A7 revisa o vídeo final e sua originalidade; A8 cobre a thumbnail. Com `CONTENT_THUMBNAIL_REVIEW_ENABLED=true` junto de A7, a aprovação A7 abre uma nova approval sobre o conjunto exato de PNGs A3 da mesma run. A página `no-store` reconfere e serve cada candidato localmente, o formulário aceita exatamente uma escolha validada no servidor, e a decisão persiste ID + SHA-256 antes de concluir com `published=false`. Rejeição cancela; adulteração, ID fora do conjunto, digest/payload divergente e replay falham com segurança. A run 4 exerceu os quatro gates e concluiu com a escolha do artifact de thumbnail 15, sem upload. Os defaults seguem `false`; o gate específico de publicação permanece pendente e nenhum upload existe.
- [x] Começar com export local. Runs concluídas pelos gates A7/A8 oferecem no dashboard
  loopback um manifesto JSON canônico e downloads do MP4 e, quando selecionado, do PNG.
  Cada GET reconstrói as approvals, a decisão A8 e os SHA-256 antes de servir; adulteração
  ou omissão da evidência A8 existente responde 409, caminhos locais nunca aparecem e
  `published=false` permanece explícito. A verificação A7 é reutilizada dentro da mesma
  requisição para evitar um segundo hash integral do MP4.
- [ ] Adicionar upload privado por API oficial, com gate próprio e autorização explícita.
- [ ] Coletar analytics, custo e resultado por conteúdo. O detalhe da run já consolida, com
  precisão decimal e isolamento por run, os indicadores editoriais A1→A6 allowlisted, a energia
  estimada e o ledger local por moeda (custo, receita, valor atribuído e receita líquida
  observada). A projeção não expõe source, category ou dimensions e declara que nenhuma plataforma
  externa foi consultada. Analytics pós-publicação por API oficial, freshness do connector e
  atribuição de resultado real continuam pendentes; nenhuma publicação ou chamada externa foi
  adicionada.
- [ ] Validar modelos, FFmpeg, Ollama e consumo de VRAM. O caminho short opt-in passou com Ollama, Kokoro/CUDA e FFmpeg reais na run 4; carga prolongada, formato long e limites sustentados de VRAM continuam `bloqueado local`.

**Portão:** uma pauta manual gera um pacote de vídeo revisável; nada é publicado sem aprovação; falhas e custos aparecem no dashboard.

## Fase 5 — Operação doméstica confiável

Objetivo: deixar o sistema rodar no computador principal com recuperação previsível.

- [x] Definir startup e shutdown seguros no Windows. O supervisor singleton executa preflight e migrations, inicia a topologia fixa em processos ocultos, usa shutdown cooperativo por eventos nomeados e Job Object como fallback, protege o status contra reutilizacao de PID e para somente os servicos Compose que iniciou, sem remover volumes. O ciclo completo passou em Windows real com PostgreSQL persistente, Redis, dashboard, Beat e três workers; stop/start cooperativo em idle preservou os serviços Compose não pertencentes ao supervisor.
- [x] Implementar health checks locais redigidos e com timeout limitado. O probe real dos três workers no Windows mantém o timeout interno do Celery e uma margem externa limitada para o overhead do inspect; duas observações saudáveis resolveram automaticamente o alerta durável criado pelo falso timeout anterior.
- [x] Transformar degradações persistentes de health em alertas locais deduplicados. Duas observações degradadas abrem ou atualizam o alerta e duas saudáveis o resolvem; `skip` não altera o tratamento. Estado e eventos ficam no PostgreSQL, a task roda na fila `io` e não envia notificações externas.
- [x] Criar backup e restore de banco, configuração e artefatos. O bundle versionado publica snapshot, configuração allowlisted, artefatos e manifesto SHA-256 atomicamente. Restore exige confirmação exata, runtime parado, storage vazio e backup automático prévio; SQLite usa substituição atômica e PostgreSQL usa transação única. Um ciclo real de backup/restore passou em PostgreSQL 17 e volume Compose descartáveis; o backup verificado também passou contra o volume persistente doméstico, enquanto o restore desse volume continua `bloqueado local`.
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
- [x] Testar reinício durante task, perda de Redis, falta de disco e indisponibilidade do Ollama por simulações locais. Lease expirada permite reclaim auditado, tentativa `running` abandonada vira falha estruturada e entrega duplicada terminal não cria outro step. Perda de Redis entre sessões deixa a mesma entrega `pending`, e a republicação posterior preserva uma única run/step. `ENOSPC` e conexão recusada pelo Ollama falham com erro redigido/retryable, sem artifact ou approval parcial. PostgreSQL persistente, Redis, Ollama e os três workers reais passaram no runtime doméstico, inclusive uma run durável sem efeito externo e um stop/start em idle; interrupção durante task, perda real do Redis nesse volume e ENOSPC em filesystem real continuam `bloqueado local`.
- [x] Documentar operação e troubleshooting locais. `docs/operations/local-recovery.md`
  explica diagnóstico e recuperação reversível de Redis, Ollama, reinício e
  falta de espaço, sem recriar runs ou tocar em dados. O stop/start real em idle passou contra o
  volume PostgreSQL doméstico; reinício durante task e ENOSPC em filesystem real permanecem
  `bloqueado local`.

**Portão:** após reinício ou falha simulada, o estado permanece consistente, não há execução duplicada e existe recuperação documentada.

## Fase 6 — Segundo módulo orientado a valor

Objetivo: provar que Automation Foundry é plataforma, não apenas um pipeline de vídeo.

Avaliar candidatos por valor esperado, qualidade de dados, risco, esforço e manutenção:

1. inteligência de mercado e concorrentes;
2. monitor de preços, promoções ou oportunidades;
3. pesquisa e qualificação de leads com rascunhos aprováveis;
4. geração recorrente de relatórios e briefs.

- [x] Selecionar um único candidato com hipótese e métrica de sucesso. Escolhido: relatórios e briefs recorrentes, pela comparação em `docs/planning/phase-6-candidates.md` (menor risco, sem credencial, primeiro produtor real de `ConnectorObservation`). Hipótese e métrica estão propostas, mas o baseline manual, o tema e as fontes dependem do proprietário e precisam ser fechados antes da especificação.
- [x] Especificar pelo contrato `$build-automation-module`. A especificação do módulo `operations-brief` (tema: a operação do próprio Automation Foundry) está em `docs/planning/operations-brief-spec.md`.
- [x] Implementar primeiro uma execução manual. `src/briefs/` implementa a primeira fatia: uma run manual na fila `io` lê somente estado persistido, gera evidência JSON redigida e um brief Markdown determinístico, registra dois artifacts (retenção de 180 dias), três métricas, custo externo zero e uma `ConnectorObservation` (`platform-database`), e abre a approval `review_operations_brief` com página de revisão completa que reconfere SHA-256 e reconstrói o texto a partir da evidência. Aprovar conclui localmente; rejeitar cancela; adulteração bloqueia. Falha de disco e de persistência, replay e cancelamento têm testes. Validado ponta a ponta com PostgreSQL, Redis e worker `io` reais (run 7). Sem schedule, sem narrativa por LLM e sem envio de nada.
- [x] Integrar estado, aprovações, custos e métricas ao dashboard. O formulário de disparo, o detalhe da run (steps, approval, artifacts, métricas, ledger), a fila de approvals com link para a revisão completa e o painel de connectors, que agora exibe a primeira observação real, usam os contratos compartilhados sem página própria de domínio além da revisão.
- [ ] Avaliar o experimento antes de automatizar o schedule. Pendente: por decisão do proprietário em 2026-10-09, o schedule semanal (`Brief semanal`, segunda 08:00 em America/Sao_Paulo) foi habilitado antes desta avaliação. Cada ocorrência continua passando pela approval humana e não envia nada; a avaliação da hipótese segue dependendo do baseline manual.

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
