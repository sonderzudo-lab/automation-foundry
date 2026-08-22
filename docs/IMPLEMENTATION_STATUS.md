# Estado real da implementação

**Checkpoint:** 2026-08-13

**Escopo:** auditoria do roadmap e fatias locais verificáveis das Fases 0 a 5

**Branch:** `chore/project-skills`

## Veredito

O repositório possui um kernel local testável e observável e um supervisor Windows para o runtime
compartilhado. A configuração, o banco, o dispatch Celery, os schedules, o control plane e o pipeline
local A1→A8 têm código real, embora A2→A8 ainda dependam de backends opt-in, mídia sintética ou
revisão local para sua qualificação. O sincronismo produzido por A4 agora tem um diagnóstico local
auditável e somente leitura, que mede a energia do próprio WAV aprovado contra os eventos do ASS
publicado sem alterar a run. O kernel compartilhado persiste `Automation`, `Run`, `StepRun`,
`Approval`, `Artifact`, `Schedule`, `MetricPoint`, `LedgerEntry`, `Alert`, controles de execução e
cada transição de estado. O dashboard permite operar runs, approvals e schedules locais, mostra saúde redigida e
custos/resultados observados, inclusive um resumo local por run do Content Engine. Publicação,
analytics de plataforma, engajamento, monetização, Trading Research Lab e agentes
independentes continuam placeholders ou planejados.

## Progresso estimado

O checklist literal do `ROADMAP.md` continua em **39 de 67 itens concluídos (58,2%)**: o diagnóstico
de alinhamento A4 fortalece o item de visuais/legendas/montagem, mas não o fecha, porque narração
real, pacote visual com direitos revisados, formato long e revisão editorial seguem pendentes.
Considerando crédito parcial somente para contratos com código e testes — principalmente A2→A8,
cujos itens ainda ficam abertos por validações reais e escopo comercial — a estimativa de maturidade
técnica é **cerca de 61%**, com faixa prudente de **59% a 63%**. Isso mede entrega do roadmap, não
esforço nem prazo.

| Fase | Checklist | Leitura atual |
| --- | ---: | --- |
| 0 — Fundamentos | 5/6 (83%) | Falta revisão/merge do usuário. |
| 1 — Baseline | 6/7 (86%) | Falta fechar validações bloqueadas no computador doméstico. |
| 2 — Kernel | 11/11 (100%) | Contrato compartilhado e dispatch durável entregues. |
| 3 — Control plane | 8/8 (100%) | As ações mínimas do dashboard e a observabilidade de connectors estão cobertas; futuros executores de domínio devem aderir aos mesmos contratos. |
| 4 — Content Engine | 2/9 (22%) literal | A2→A8 têm fatias reais e testadas, o export local verificado, o resultado local por run e o diagnóstico auditável de alinhamento A4 estão disponíveis; upload/publicação, mídia real qualificada e analytics externos continuam abertos. |
| 5 — Operação doméstica | 7/7 (100%) | Gate documental cumprido; ensaios físicos ainda marcados como bloqueados onde aplicável. |
| 6–8 — Trading, revenue e agentes | 0/19 (0%) | Domínios ainda não implementados. |

## Classificação por área

| Área | Estado | Evidência e limite atual |
| --- | --- | --- |
| Identidade técnica | Implementado | `pyproject.toml`, `.env.example`, `src/core/config.py` e `docker-compose.yml` usam `automation-foundry` nos identificadores ativos. “Content Engine” permanece como nome legítimo do módulo. |
| Configuração | Parcial | `src/core/config.py` implementa settings tipados, leitura de `.env` e defaults locais. Campos de credenciais existem, mas criptografia, validação operacional e adapters consumidores ainda não existem. |
| Banco e sessão | Implementado no caminho local atual | `src/core/database.py` cria engine e sessão async com commit/rollback. PostgreSQL usa `asyncpg`, pool limitado, pre-ping e timeouts de conexão/comando; somente URLs em loopback são aceitas. SQLite permanece como default sem `.env`, bootstrap de processo único e backend dos testes. O dispatch concorrente foi validado em PostgreSQL. |
| Modelos de domínio | Parcial | `src/core/models.py` implementa `Channel`, `Video`, `Job`, `Metric`, `Cost`, `Topic`, `Alert` e `ABVariant`. O schema é específico do Content Engine e não contém os contratos genéricos da Fase 2. |
| Kernel compartilhado | Parcial | `src/platform/models.py` implementa `Automation`, `Run`, `StepRun`, `RunDispatch`, `Approval`, `Artifact`, `Schedule`, `ScheduleOccurrence`, `MetricPoint`, `Experiment`, `LedgerEntry`, `Alert`, `HealthCondition`, `ConnectorObservation`, controles persistidos e históricos append-only. `Approval.decision_payload` persiste escolhas estruturadas pequenas, não sensíveis e imutáveis. Cada horário de schedule possui resultado `pending`, `published` ou `skipped`; dispatch possui identidade estável, tentativas de publicação e claim/lease. Observações de connector guardam somente chave segura, estados allowlisted, SLOs e timestamps/score normalizados. |
| Migrations | Implementado no baseline atual | `alembic.ini`, `alembic/env.py` e vinte e duas revisions incrementais formam um caminho verificável para as tabelas compartilhadas e as oito tabelas legadas do Content Engine. A revision 0021 adiciona `Approval.decision_payload` e a 0022 cria `connector_observations`; `platform_alerts` evita colisão com `alerts`, e os roundtrips são testados em SQLite. Todas as revisions, incluindo 0021/0022, e `alembic check` passaram em PostgreSQL 17 descartável. `health_conditions` e `connector_observations` agora pertencem explicitamente ao escopo do autogenerate; remover qualquer uma faz o check falhar. Em SQLite migrado, `quality_score` usa texto canônico compatível com `_ExactNumeric`; PostgreSQL usa `NUMERIC(5,4)`. Evoluções futuras de schema ainda exigem novas migrations. |
| Content Engine — A1 | Implementado, com validação real pendente | `src/pipeline/script_gen.py` mantém a geração em quatro chamadas, retry limitado e saneamento de saída. `src/pipeline/a1_executor.py` adiciona entrada tipada, run/step `gpu`, bundle JSON atômico e revisável em `STORAGE_ROOT`, artifact interno com retenção de 90 dias, métrica de tamanho e observação de custo de API externa zero. Depois da geração, a run aguarda approval editorial ligada ao ID e SHA-256 do bundle; a página de revisão mostra o resultado completo sem persona ou contexto privado, rejeição cancela e arquivo alterado reverte a decisão. Com TTS desabilitado, aprovação íntegra conclui A1; uma configuração futura habilitada acordará a mesma run para A2. O dashboard prepara dispatch durável e retry explícito sem bloquear a requisição; A1 não publica, envia mensagens nem gera gasto externo. Sucesso, idempotência, falha retryable, rollback da persistência parcial de evidências, retry, cancelamento, approval e entrega duplicada usam gerador mockado. Falta de disco vira `CONTENT_STORAGE_WRITE_FAILED` retryable sem artifact/approval parcial, e conexão recusada do Ollama vira `CONTENT_LLM_UNAVAILABLE` sem detalhe de endpoint; Ollama e GPU reais continuam pendentes no computador local. |
| Content Engine — TTS A2 | Parcial, teste local opt-in | `src/pipeline/tts.py` exige a approval exata do bundle A1, cria step `gpu` ordinal 2, recebe um adapter substituível, valida WAV PCM S16LE mono, publica por rename atômico e registra áudio interno com retenção de 90 dias, duração, energia estimada, proveniência/licença e custo externo zero. A mesma identidade de dispatch pode ser reaberta com evento `requeued` após a approval e entregas duplicadas não repetem A1 nem A2. O adapter `kokoro_quality_test` fixa Kokoro 0.9.4, revisão/peso/voz, exige CUDA e eSpeak NG externo, limita chunks e marca a voz como `provenance-unverified`/`quality-test-only`; `disabled` permanece o default. Na RTX 3090 local, PyTorch 2.7.1+cu128, CUDA 12.8, eSpeak NG e Kokoro 0.9.4 geraram o teste opt-in e três amostras curtas (`pf_dora`, `pm_alex`, `pm_santa`) em WAV mono 24 kHz. Retry, saída inválida, payload divergente e rollback de evidências usam fake. Escuta humana e decisão comercial continuam pendentes; nada habilita publicação ou uso comercial. |
| Content Engine — visuais A3 | Parcial, importação local opt-in | `src/pipeline/visuals.py` consome o bundle A1 aprovado e o WAV A2 verificados por hash, calcula um visual a cada seis segundos, exige proveniência, licença, atribuição e direito comercial por asset, valida PNG completo nas dimensões short/long, publica imagens + manifesto por rename atômico e registra artifacts, contagem, cobertura e custo externo zero. Quando A3 está habilitado, A2 preserva a run em `running` e A3 a conclui sem alterar o áudio. Além dos fakes, `local_assets_quality_test` importa na fila `io` somente arquivos selecionados pelo operador dentro de raiz configurada, com manifesto estrito, quantidade exata, IDs únicos e SHA-256 por item; não baixa, repete nem altera a origem. `disabled` permanece o default, o pacote ainda exige revisão humana e a declaração de direitos não é prova jurídica independente. FLUX.1 e provedores stock seguem bloqueados. |
| Content Engine — legendas A4 | Parcial, timing local opt-in | `src/pipeline/captions.py` consome o WAV A2 conferido por tamanho/SHA-256 e a narração ligada à approval A1, aceita somente timestamps por palavra monotônicos dentro da duração, idioma PT, similaridade mínima, proveniência/licença e `commercial_use=True`. Publica ASS com karaoke por rename atômico e registra artifact, palavras temporizadas, similaridade, energia GPU quando aplicável e custo externo zero. Além dos fakes, `approved_text_timing_quality_test` roda em `cpu` e distribui deterministicamente o texto aprovado pela duração real do WAV, sem modelo ou download. Ele viabiliza montagem local, mas não escuta o áudio, não detecta pausas/deriva e exige revisão humana. A deriva que ele não detecta passou a ser mensurável fora do step pelo diagnóstico auditável de alinhamento descrito na linha seguinte. Faster-whisper e pesos continuam bloqueados. |
| Content Engine — diagnóstico de alinhamento A4 | Implementado, somente leitura | `src/pipeline/caption_alignment.py` publica evidência auditável sobre o sincronismo já produzido, sem alterar o pipeline. Ele exige que a run pertença à automação `content-engine`, seleciona o `narration_audio` e o `caption_ass` daquela run, reconfere confinamento em `STORAGE_ROOT`, tamanho e SHA-256 dos dois, lê o WAV em uma única passada com janelas de 20 ms, calcula RMS por janela, deriva o limiar como `pico − 30 dB` com piso em −55 dBFS, une janelas com gap ≤ 0,30 s e descarta trechos abaixo de 0,12 s. Os eventos `Dialogue` do ASS são reunidos em blocos pela mesma regra de gap. O resultado traz cobertura da fala, fração de legenda sobre silêncio, desvio de início e de fim, desvio máximo por bloco quando as contagens coincidem, contagem de trechos/blocos/eventos e eventos além da duração do áudio, tudo em `Decimal`. Status é `pass` ou `warning`, com códigos fechados (`LOW_SPEECH_COVERAGE`, `CAPTIONS_OVER_SILENCE`, `ONSET_OFFSET`, `END_OFFSET`, `BLOCK_ONSET_OFFSET`, `EVENTS_BEYOND_AUDIO`); `warning` abre um alerta local deduplicado por run e não interrompe nada. O relatório JSON canônico vira artifact `caption_alignment_report` com 90 dias de retenção, e cinco métricas allowlisted ficam atribuídas à run sem step. A chave idempotente e o nome do arquivo carregam o digest dos parâmetros mais os dois hashes: repetir com a mesma evidência e configuração é replay exato, e outra configuração gera um relatório separado em vez de sobrescrever. Run inexistente, run de outra automação, artifact ausente, evidência adulterada, ASS sem eventos ou com tempo inválido e WAV que não seja PCM S16LE mono falham fechado antes de qualquer persistência. `automation-foundry caption-alignment --run-id N [--json]` expõe o diagnóstico localmente, retorna 1 em `warning` e imprime somente códigos fechados em caso de falha. O detalhe da run reexibe o último relatório sem depender do terminal: `load_caption_alignment_report` reconfere tamanho e SHA-256 do arquivo, valida `schema_version`, algoritmo, `run_id`, `published=false`, `external_service_consulted=false`, status em `pass`/`warning`, motivos dentro do conjunto fechado e cada número por tipo, e devolve uma projeção allowlisted. Ausência de relatório omite a seção; adulteração do conteúdo ou do arquivo vira aviso seguro sem derrubar a página; caminho de storage e raiz absoluta nunca são renderizados. Nada é publicado, enviado, apagado ou pago; o diagnóstico mede sincronismo observável e não substitui escuta humana nem alinhamento forçado por modelo. |
| Content Engine — montagem A5 | Parcial, quality-test real opt-in | `src/pipeline/assembly.py` confere WAV, manifesto/imagens e ASS por tamanho/SHA-256, reconcilia o manifesto com cada artifact visual e executa na fila `cpu`. O resultado precisa declarar H.264, AAC, legendas queimadas, proveniência/licença e direito comercial; o parser interno e ffprobe validam estrutura, tracks, resolução e duração. Além dos adapters fake, `ffmpeg_quality_test` exige hashes exatos de FFmpeg/ffprobe externos, confirma flags GPLv3/libx264/libass, usa subprocess sem shell, timeout e stderr não persistido. O build Gyan 8.0.1 instalado por WinGet gerou um short 1080×1920 H.264/AAC reproduzível e a legenda foi confirmada em frame. O default continua `disabled`; não há redistribuição dos binários, e áudio real, long, revisão editorial e política de distribuição continuam pendentes. |
| Content Engine — originalidade A6 | Parcial, gate lexical implementado | `src/intelligence/similarity.py` verifica o bundle A1 aprovado e o MP4 A5 por tamanho/SHA-256, compara unigramas por cosseno e trigramas por Jaccard na fila `cpu` e usa somente runs anteriores da mesma automação que já possuem relatório A6 e terminaram com sucesso. Limite e janela são configuráveis; relatório JSON, máximo/média, contagem, duração, energia estimada e custo zero ficam atribuíveis. Score máximo no limite ou acima abre alerta `warning` e falha a run com `CONTENT_ORIGINALITY_BLOCKED`, sem upload ou override automático. Duplicata, texto diferente, adulteração histórica, replay e rollback de evidências possuem testes. O escopo por canal foi investigado neste checkpoint e **não** foi implementado: `channels` existe apenas como tabela legada do schema original, nenhum código do runtime cria, lê ou vincula um canal, `Run` não possui coluna nem payload validado de canal, e a identidade de canal está atrelada a plataforma e credencial de publicação — escopo explicitamente adiado. Restringir a janela exigiria aceitar um identificador arbitrário sem contrato durável validado no servidor, o que contraria o fail-closed do gate, então a decisão fica registrada como produto pendente em vez de fabricada. Detecção semântica de paráfrases também permanece pendente. |
| Content Engine — revisão final A7 | Parcial, gate humano local implementado | `src/pipeline/final_review.py` adiciona o gate humano do vídeo final. Com `CONTENT_FINAL_REVIEW_ENABLED=true`, A6 deixa de concluir a run e A7 confere o MP4 e o relatório de originalidade por tamanho e SHA-256, exige que o relatório descreva exatamente aquele vídeo e que `blocked` seja falso, e abre uma approval `review_content_final_video_a7` ligada ao digest dessas evidências. A projeção visível traz resolução, duração, bytes, codecs, os dois SHA-256, os números de originalidade e a frase explícita de que nada é publicado; persona, prompt e contexto privado ficam fora. Aprovar conclui a run com `published=false` quando A8 está desabilitado, ou abre A8 na mesma run quando ambos estão habilitados; arquivo alterado após o pedido recusa a decisão sem transicionar a run; rejeitar cancela a run pelo contrato compartilhado. O default continua `false`, o replay de entrega duplicada não repete trabalho e nenhum upload existe. `/approvals/<id>/review` renderiza a página `no-store` dedicada com resolução, duração, bytes, codecs, os dois SHA-256, algoritmo, limite, similaridades e as comparações históricas redigidas; `/approvals/<id>/final-video` transmite o MP4 exato pelo próprio dashboard, com `no-store`, `nosniff` e suporte a Range, somente depois de reconferir hash e digest. Approval de outra ação recebe 404 e evidência adulterada recebe 409 nas duas rotas. O caminho do storage nunca é renderizado. |
| Content Engine — thumbnail A8 | Parcial, gate humano local implementado | `src/pipeline/thumbnail_review.py` adiciona o gate de thumbnail, desabilitado por default e permitido somente junto de A7. Depois da aprovação íntegra de A7, a mesma run recebe a approval `select_content_thumbnail_a8`, cujo digest congela os IDs, tamanhos e SHA-256 dos `visual_image` PNG produzidos pelo step A3 daquela run. Cada leitura reconfere step/origem/tipo, confinamento em `STORAGE_ROOT`, tamanho, assinatura PNG e hash. A página própria `no-store` serve os candidatos por rotas locais `nosniff` e exige exatamente um radio button; o ID recebido é comparado ao conjunto reconstruído no servidor e a decisão imutável persiste `thumbnail_artifact_id` + `thumbnail_sha256`. Aprovar conclui localmente com as approvals A7/A8 e `published=false`; rejeitar cancela. ID arbitrário ou de outra run, arquivo adulterado, digest/payload ausente ou divergente e replay não concluem nem duplicam trabalho. A CLI genérica recusa aprovação positiva A8 sem a escolha do dashboard. Nenhum upload ou publicação existe; o export local verificado vive em `src/pipeline/local_export.py`. |
| Content Engine — ensaio local A1→A6 | Parcial, ensaio com mídia sintética | `tests/support/local_media_rehearsal.py` gera toda a mídia do ensaio dentro do repositório: um WAV determinístico de tom puro identificado como `synthetic_quality_test_fixture`, PNGs sintéticos com manifesto A3 estrito e SHA-256 por item, e um roteiro PT-BR fixo. `tests/pipeline/test_local_media_rehearsal.py` roda na suíte padrão e exercita os backends reais `local_assets_quality_test` (A3) e `approved_text_timing_quality_test` (A4) dentro da cadeia compartilhada, com A5 fake e A6 real, conferindo ordem/filas dos seis steps, tipos de artifact, reconciliação de hashes, cobertura do ASS sobre a duração real do WAV e integridade da raiz de importação; o mesmo teste roda em seguida o diagnóstico de alinhamento A4 sobre a evidência produzida e obtém `pass` com um único trecho de fala, o que prova a integração mas não sincronismo contra voz real, porque a fixture é um tom contínuo; um segundo teste prova o bloqueio fail-closed quando o manifesto diverge da quantidade calculada. `tests/integration/test_local_media_rehearsal.py` é opt-in por `AUTOMATION_FOUNDRY_TEST_FFMPEG=1` mais os dois hashes fixados e executa a mesma cadeia com FFmpeg real. Nenhum download, chamada de rede, API externa ou asset de terceiro participa; nada é publicado. O ensaio não valida narração real, direitos sobre imagens reais, sincronismo contra voz real, formato long nem adequação editorial. Proveniência e limites estão em `docs/licensing/content-rehearsal-fixtures.md`. |
| Content Engine — export local | Implementado, somente loopback | `src/pipeline/local_export.py` reconstrói a approval A7 e consulta o banco para distinguir uma run realmente A7-only de uma run cuja evidência A8 foi omitida do output. Quando A8 existe, approval, decisão e as três chaves são obrigatórias; adulteração, omissão ou divergência responde 409. A evidência A7 já verificada é reutilizada por A8 dentro da mesma requisição, evitando o segundo hash integral do MP4. As quatro rotas locais respondem `no-store`, os arquivos usam `nosniff` e o template recebe somente IDs, tamanhos e hashes — nunca os objetos que carregam caminhos. O export não copia arquivos, não cria ZIP e mantém `published=false`. |
| Content Engine — resultado local por run | Parcial, projeção local implementada | O detalhe da run consolida somente nomes, tipos e unidades allowlisted das métricas A1→A6 e do diagnóstico de alinhamento A4 — estas últimas aparecem no escopo `run`, sem step, e preservam o sinal negativo dos desvios —, soma as estimativas de energia válidas e agrega `LedgerEntry` da mesma run por moeda com `Decimal`, separando custo, receita, valor atribuído e receita líquida observada. Métrica desconhecida ou com contrato divergente não entra no resumo; ledger de outra run também não entra. `source`, `category`, `dimensions` e chaves idempotentes permanecem fora da projeção, que mostra o último horário de evidência e declara que nenhuma plataforma externa foi consultada. Não há conversão cambial, coleta externa, pagamento ou promessa. Analytics pós-publicação, freshness e atribuição de resultado real continuam pendentes. |
| Content Engine — publicação | Placeholder | `src/pipeline/upload.py` está vazio. Nenhum upload ou publicação real foi implementado. |
| Orquestração e filas | Parcial | `src/core/celery_app.py` declara exclusivamente `gpu`, `cpu` e `io`, usa mensagens JSON persistentes, late ack, prefetch 1, publish retry limitado e timeouts configurados. `RunDispatch` protege a entrega e `ScheduleOccurrence` protege cada horário previsto. Beat publica ticks de schedule e health na fila IO. O entrypoint combina PID file e named mutex no Windows; uma segunda aquisição do mutex é recusada. |
| CLI operacional | Parcial | `src/cli.py` expõe diagnóstico, dashboard operacional, execução inline, `enqueue-run`, controles, approvals, artifacts, schedules, métricas, ledger, alertas e `backup`/`verify-backup`/`restore-backup`. `caption-alignment --run-id N` executa o diagnóstico somente leitura do sincronismo A4 e devolve 1 quando o alinhamento sai da tolerância; `retention-policy` imprime a política declarativa de retenção sem tocar em storage. `automation-foundry-runtime start/stop/status` opera a topologia fixa; os entrypoints de worker e Beat continuam disponíveis para diagnóstico. A aprovação A8 é recusada na CLI porque exige seleção estruturada na página local; rejeição continua disponível. A CLI local ainda não autentica criptograficamente o actor. |
| Control plane | Parcial | `src/dashboard/main.py`, `service.py` e os templates implementam visão geral, detalhe da run e saúde com `no-store`, schedules e saúde operacional redigida. Schedules aparecem em ordem operacional com estado persistido/efetivo, cron, timezone, próxima execução, última ocorrência e somente diagnósticos allowlisted; o formulário local permite habilitar/desabilitar futuros disparos com CSRF, confirmação, actor, motivo e evento append-only. Habilitar exige automação ativa, kill switch livre e executor registrado, recalcula `next_run_at` e usa lock de linha no PostgreSQL; double-click concorrente e replay sequencial produzem no máximo um evento. Habilitação administrativa, kill switch e cancelamento de run também releem a linha sob lock, preservando um único evento em disputa no PostgreSQL. Bloqueios esperados retornam ao dashboard com aviso seguro, e desabilitar continua possível sob kill switch. CSRF Unicode é recusado com 403, não 500. A fila mostra a idade calculada no servidor e liga A1, A7 e A8 diretamente às páginas `no-store` que reconferem as evidências; nelas, o operador aprova ou rejeita com actor, motivo, confirmação e CSRF, e uma decisão registrada torna a página somente leitura. A8 exige ainda a escolha estruturada. Runs concluídas pelos gates exibem um export local com manifesto e downloads novamente verificados. O ledger mostra totais decimais exatos globais e por automação/moeda, incluindo receita líquida observada, sem conversão cambial nem exposição de categoria ou fonte; runs do Content Engine ganham ainda um resumo isolado com indicadores editoriais allowlisted, energia estimada e esses totais por conteúdo, mais o último relatório de alinhamento A4 reconferido por SHA-256 e validado contra conjuntos fechados antes de ser exibido. Taxa de sucesso e duração média usam uma janela de até 50 resultados `succeeded`/`failed` por automação. O bloco de connectors seleciona somente a observação persistida mais recente por automação/chave, prioriza `unavailable`/`degraded`, sinaliza truncamento, mostra UTC absoluto, calcula status efetivo e `fresh`/`stale` no servidor e atualiza a cada 30 s por HTMX `no-store`; endpoint, credencial, idempotency key e erro bruto não entram na projeção. `Automation.enabled` possui toggle idempotente e auditado; o dashboard mostra experiments redigidos e permite disparos somente por executores registrados. Cancelamento e kill switch usam POST com CSRF, Host loopback e confirmação. O detalhe atualiza runs não terminais por HTMX vendorizado; `/storage` mostra apenas totais de retenção. Adapters reais ainda precisam produzir observações, futuros executores de domínio precisam aderir aos controles e não há análise estatística de resultado. A identidade do actor local ainda não é autenticada. |
| Connectors e qualidade de dados | Contrato implementado, integrações reais pendentes | `src/platform/connector_service.py` valida e grava observações append-only idempotentes com `healthy`, `degraded`, `unavailable` ou `disabled`, SLO do check, último sucesso, SLO de freshness e qualidade `pass`, `warning`, `fail` ou `unknown`, com score opcional exato entre 0 e 1. O schema não possui endpoint, credencial, payload nem mensagem de erro. Replay divergente, timestamp ingênuo ou posterior ao relógio do servidor, slug inseguro, SLO inválido e score incompatível falham antes da persistência. Uma corrida PostgreSQL da mesma chave usa savepoint: conteúdo idêntico retorna replay e divergência retorna conflito de domínio, sem `IntegrityError` bruto. O índice latest inclui o ID de desempate, mas a tabela continua append-only sem política de retenção; nenhuma exclusão automática foi adicionada. Nenhuma API ou connector externo foi chamado nesta fatia. |
| Inteligência | Parcial | `src/intelligence/similarity.py` implementa o gate lexical A6. `src/intelligence/trends.py` e `competitors.py` continuam vazios; embeddings semânticos, tendências e inteligência competitiva não foram implementados. |
| Engajamento | Placeholder | `src/engagement/comments.py` está vazio. |
| Operações | Parcial | `health.py` coleta saúde redigida e `health_alerts.py` persiste consecutividade e alertas allowlisted. `backup.py` cria, verifica e restaura bundles SQLite/PostgreSQL com configuração sem segredos, ZIP de artefatos e manifesto SHA-256. Restore exige confirmação exata, runtime parado, storage vazio e backup automático prévio. `retention.py` inventaria `STORAGE_ROOT` somente para leitura, e `purge.py` implementa o expurgo local em dois atos, desabilitado por default e protegido por approval, backup e confirmação exata. `docs/operations/local-recovery.md` documenta diagnóstico e recuperação reversível de Redis, Ollama, reinício e falta de espaço, deixando os ensaios de worker/volume doméstico e ENOSPC real marcados como bloqueados localmente. Histórico temporal de métricas e notificação externa permanecem ausentes. `seo.py` e `repurpose.py` continuam vazios. |
| Retenção de storage | Parcial, inventário dry-run e política declarativa implementados | `src/operations/retention.py` percorre `STORAGE_ROOT` uma vez, sem seguir links e sem ler conteúdo, e reconcilia cada arquivo com os `Artifact` registrados por caminho relativo normalizado. As classes são `retained` (dentro da retenção ou sem política), `retention_hold` (retenção vencida mas não elegível), `expired`, `orphan` (arquivo sem registro), `missing` (registro sem arquivo) e `unsafe`. O hold é fail-closed e cobre run em `queued`/`running`/`awaiting_approval`, approval ainda `pending` na mesma run, tamanho em disco diferente do registrado e caminho referenciado por mais de um artifact. Caminho registrado absoluto, com `..` ou fora da raiz vira `unsafe` sob um rótulo `<artifact:id>` que não repete o valor armazenado; symlinks de arquivo e de diretório viram `unsafe` sem serem percorridos; raiz symlink e raiz de drive são recusadas; a raiz ausente é relatada sem ser criada. `backups`, `runtime` e `.gitkeep` ficam fora da reconciliação e entram apenas como total excluído. Uma varredura acima de `RETENTION_INVENTORY_MAX_FILES` levanta erro em vez de entregar resultado parcial. O resultado carrega `dry_run=True`, `deleted_file_count=0` e `deleted_bytes=0`, contagem e bytes por classe e totais por automação. `automation-foundry storage-inventory [--json] [--max-items N]` é dry-run por default e recusa `--no-dry-run` com código 2; falhas são redigidas ao nome do tipo da exceção. `/storage` renderiza `no-store` somente os totais por classe e por automação, nunca um caminho de arquivo, e degrada para uma mensagem segura quando o inventário falha. O inventário em si não persiste `MetricPoint`; o expurgo aprovado registra `storage.purged_files` e `storage.purged_bytes` na automação `platform-storage-retention`. |
| Política declarativa de retenção | Implementado | `src/operations/retention_policy.py` é a única fonte da retenção por tipo de artifact. A tabela é validada na importação (tipo único e trimado, dias ≥ 1 ou indefinido, produtor real, justificativa não vazia) e declara hoje `script_bundle`, `narration_audio`, `visual_manifest`, `visual_image`, `caption_ass`, `caption_alignment_report`, `final_video` e `originality_report`, todos com 90 dias — exatamente os valores que os produtores já usavam, de modo que esta fatia não altera a elegibilidade de expurgo de nada. A1→A6 e o diagnóstico de alinhamento resolvem a constante do módulo pela política, e `register-artifact` aplica o mesmo default quando `--retention-days` é omitido. Tipo não declarado resolve para retenção indefinida: `retention_days=None` significa que o inventário o classifica como `retained` para sempre e ele nunca entra em um plano de expurgo por omissão. A política é prospectiva por construção: `register_local_artifact` congela `retention_until` na linha, então mudar a tabela nunca antecipa o vencimento de um artefato já registrado. `automation-foundry retention-policy [--json]` é somente leitura e a página `/storage` renderiza a declaração completa mesmo quando o inventário falha. Um teste de guarda varre `src/` e recusa qualquer `artifact_type` literal que não esteja declarado, e outro fixa os 90 dias atuais para que uma redução futura seja uma decisão explícita e revisada. Retenção por automação, por sensibilidade ou por idade de run e expurgo agendado continuam fora do escopo. |
| Expurgo aprovado de storage | Implementado, desabilitado por default | `src/operations/purge.py` é o único caminho do projeto que apaga dados do operador, e `RETENTION_PURGE_ENABLED` continua `false`. `plan_retention_purge` seleciona apenas itens `expired` do inventário, relê e re-hasheia cada arquivo, e falha o plano inteiro em vez de propor um subconjunto; o plano canônico fica em `run.input_payload` da automação `platform-storage-retention` e a approval `purge_expired_artifacts` é ligada ao digest desse plano exato. A projeção revisável traz ação, contagem, bytes, automações, tipos, sensibilidade, digest e a frase de irreversibilidade, sem nenhum caminho de arquivo. Planejar duas vezes o mesmo conjunto retorna a mesma run e a mesma approval. `execute_approved_purge` exige a approval `approved`, o runtime parado, a confirmação exata `PURGE <plan-digest>` e o plano armazenado ainda batendo com o digest da approval; então cria um backup verificado, faz uma varredura completa de verificação que aborta antes da primeira remoção, e reconfere o SHA-256 imediatamente antes de cada `unlink`. Artefatos removidos preservam seu registro com `purged_at` e `purged_by_approval_id` (migration 0020, com check constraint de evidência completa e downgrade recusado enquanto houver expurgo registrado), aparecem como `purged` no inventário e nunca como `missing`; um arquivo que reaparece no caminho purgado vira `unsafe`. Falha após a primeira remoção grava a evidência parcial, deixa run e step `failed` e mantém o backup prévio como recuperação; a CLI retorna código 1. Replay de uma run terminal não apaga nada de novo. Rejeitar a approval cancela a run pelo contrato compartilhado. O dashboard pode aprovar o gate pela fila normal, e aprovar apenas autoriza: a remoção só acontece no comando explícito. Expurgo de órfãos, agendamento e política declarativa por tipo de artefato não existem. |
| Receita e experimentos | Parcial | O contrato compartilhado de Experiment e sua atribuição de runs estão implementados. `src/revenue/aggregator.py`, `profit.py` e `ab_testing.py` continuam vazios; não há análise estatística nem decisão automática de vencedor. |
| Extras e ações externas | Placeholder | `src/extras/affiliate.py`, `outreach.py` e `x_bot.py` estão vazios. Não há envio, gasto ou ação externa implementada. |
| Trading Research Lab | Planejado | O domínio aparece em `ARQUITETURA.md` e `ROADMAP.md`, mas não possui implementação. Trading com dinheiro real permanece fora do escopo. |
| Infraestrutura local | Parcial | `docker-compose.yml` define PostgreSQL 17 e Redis com volumes, health checks e portas publicadas somente em `127.0.0.1`. O supervisor Windows inicia três workers host, Beat e dashboard, reivindica somente serviços Compose que estavam parados e nunca remove volumes. Ollama permanece fora do Compose. |
| Testes | Parcial | A suíte cobre publicação, falha de broker, claim, lease expirada, ownership, duplicatas, health, supervisor e backup/restore. Um teste SQLite simula a morte de um worker depois de persistir a run e o step como `running`: após a lease expirar, o reclaim registra `lease_reclaimed`, converte a tentativa abandonada em `INTERRUPTED_ATTEMPT`, executa somente a tentativa 2 e rejeita a entrega duplicada terminal. Outro usa sessões SQLite separadas para simular a perda e retorno do Redis: a falha deixa a mesma run/entrega como `pending` com `BROKER_PUBLISH_FAILED`; a republicação usa a mesma identidade e a execução posterior mantém uma única run e step. A1 também simula `ENOSPC` e conexão recusada pelo Ollama, provando erros redigidos/retryable e ausência de artifact ou approval parcial. Um checkpoint real em PostgreSQL 17 e Redis 7 descartáveis repetiu as migrations e as integrações, derrubou Redis durante uma publicação e confirmou que o dispatch persistido foi reenviado com a mesma entrega, uma única run/step e rejeição da duplicata. O ciclo manual de queda e retorno do Ollama também passou: `doctor --services` marcou indisponibilidade enquanto a porta estava fechada e saúde após `ollama serve` voltar, com API HTTP 200 e cinco modelos locais. Falta de disco em filesystem real, volume doméstico, modelos e carga GPU ainda não foram validados. |

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

Com o runtime parado, o backup local e sua verificação offline usam:

```powershell
.venv\Scripts\automation-foundry backup --json
.venv\Scripts\automation-foundry verify-backup "storage/backups/<backup-id>" --json
.venv\Scripts\automation-foundry restore-backup "storage/backups/<backup-id>" --confirm "RESTORE <backup-id>" --json
```

O default publica o bundle em `STORAGE_ROOT/backups`; um destino externo pode ser informado com
`--destination`. O bundle inclui banco, configuração allowlisted e artefatos, mas exclui
`STORAGE_ROOT/runtime`, backups anteriores e `.gitkeep`. PostgreSQL é exportado por `pg_dump` dentro
do container; se o comando iniciou o serviço, ele tenta pará-lo ao final. A configuração não inclui
senhas, tokens ou URLs com credenciais. Restore é deliberadamente restritivo: o runtime precisa
estar parado, o storage de artefatos precisa estar vazio, a identidade do banco deve coincidir e a
confirmação deve conter o ID exato. Antes de substituir o banco, outro bundle preserva o estado
atual; falha do banco remove apenas os artefatos recém-instalados.
Os tasks Celery registrados são os probes sem efeito externo, o executor de dispatch durável e os
ticks de schedules e reconciliação de health. Beat apenas publica os ticks; horários, ocorrências,
consecutividade e alertas continuam no PostgreSQL.
Runs publicadas por `enqueue-run` ou pelo formulário A1 usam identidade estável, claim/lease no
PostgreSQL e replay seguro; o executor inline permanece disponível para diagnóstico local. A1 usa a
fila `gpu`, mas não publica conteúdo nem executa outro efeito externo.

Esses comandos gerenciam as vinte e duas tabelas compartilhadas: `automations`, `runs`,
`run_transitions`, `run_dispatches`, `run_dispatch_events`, `step_runs`, `step_run_transitions`, `control_events`, `approvals`,
`approval_events`, `artifacts`, `schedules`, `schedule_events`, `schedule_occurrences`, `experiments`, `experiment_events`, `metric_points`, `ledger_entries`, `platform_alerts`,
`platform_alert_events`, `health_conditions` e `connector_observations`;
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
`--expected-sha256` quando o produtor já conhece o digest. Sem `--retention-days`, o tipo resolve a
retenção pela política declarativa e um tipo não declarado fica com retenção indefinida. A política
de retenção é apenas metadado: este comando não apaga arquivos.

A declaração completa, somente leitura, é inspecionável por:

```powershell
.venv\Scripts\automation-foundry retention-policy --json
```

Depois que uma run do Content Engine publicar áudio e legenda, o alinhamento pode ser diagnosticado
localmente sem alterar a run, regenerar legenda ou consultar qualquer serviço externo:

```powershell
.venv\Scripts\automation-foundry caption-alignment --run-id 1 --json
```

O comando reconfere os dois artifacts por tamanho e SHA-256, mede a energia do WAV aprovado e
compara com os eventos do ASS publicado. A saída traz status, motivos com códigos fechados,
cobertura, sobreposição de silêncio, desvios de início/fim, contagens e o artifact do relatório;
`published` permanece `false`. O código de saída é 0 em `pass`, 1 em `warning` ou falha e 2 para
`--run-id` inválido. Evidência adulterada, ASS inválido, WAV fora do formato aceito ou run de outra
automação falham fechado, sem gravar relatório, métrica ou alerta. Repetir o comando com a mesma
evidência e a mesma configuração é replay exato; alterar
`CONTENT_CAPTION_ALIGNMENT_MIN_SPEECH_COVERAGE`, `CONTENT_CAPTION_ALIGNMENT_MAX_OUTSIDE_SPEECH` ou
`CONTENT_CAPTION_ALIGNMENT_TOLERANCE_SECONDS` gera um relatório separado em vez de sobrescrever o
anterior.

O inventário de retenção reconcilia disco e registros sem apagar, criar ou alterar nada:

```powershell
.venv\Scripts\automation-foundry storage-inventory --json
.venv\Scripts\automation-foundry storage-inventory --max-items 50
```

O modo dry-run é o default e o único suportado; `--no-dry-run` é recusado com código 2 porque a
exclusão material exige uma fatia separada com aprovação humana. A saída traz `dry_run`,
`deleted_file_count`, contagem e bytes por classe, totais por automação e os itens detalhados,
limitados por `--max-items` (default `RETENTION_INVENTORY_MAX_ITEMS`) sem afetar os totais. Os
caminhos relativos aparecem apenas no terminal local; a página `/storage` do dashboard mostra
somente os totais redigidos. Um storage maior que `RETENTION_INVENTORY_MAX_FILES` interrompe a
varredura em vez de reportar um estado parcial.

O expurgo material exige `RETENTION_PURGE_ENABLED=true`, o runtime parado e duas decisões
separadas. Nada é apagado antes do terceiro comando:

```powershell
.venv\Scripts\automation-foundry plan-purge --actor "local-owner" --reason "limpeza de storage" --json
.venv\Scripts\automation-foundry decide-approval --approval-id 1 --approve --actor "local-owner" --reason "lista revisada"
.venv\Scripts\automation-foundry execute-purge --approval-id 1 --confirm "PURGE <plan-digest>" --actor "local-owner" --json
```

`plan-purge` só considera artefatos classificados como `expired` e falha o plano inteiro se
qualquer arquivo divergir do seu SHA-256 registrado. A approval pode ser decidida pela CLI ou pela
fila do dashboard; aprovar apenas autoriza, nunca remove. `execute-purge` recusa sem approval
aprovada, com runtime ativo, com confirmação diferente do digest aprovado ou com o plano
armazenado adulterado, cria um backup verificado antes de remover qualquer byte e reconfere o hash
de cada arquivo imediatamente antes de apagá-lo. O backup prévio é o caminho de recuperação
documentado. Repetir `execute-purge` em uma run já terminal é um replay que não apaga nada.

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
O detalhe de uma run não terminal consulta `/runs/<id>/fragment` a cada dois segundos para atualizar
somente status e steps. O asset HTMX é servido por `/static/htmx.min.js`; nenhum recurso externo é
necessário e a página completa continua utilizável quando JavaScript está desabilitado.
Controles mutáveis de run, kill switch e approval exigem confirmação explícita e token CSRF por
processo. Totais financeiros são calculados com `Decimal` em Python para preservar a precisão do
SQLite e permanecem separados por moeda, sem conversão cambial.

O ensaio local A1→A6 com mídia sintética roda na suíte padrão sem nenhuma ferramenta externa. A
variante com FFmpeg real é opt-in e exige os hashes exatos do build já auditado:

```powershell
$env:AUTOMATION_FOUNDRY_TEST_FFMPEG = "1"
$env:CONTENT_FFMPEG_EXPECTED_SHA256 = "74db6c184a03dba2bdfe23e1a1f41cf5a8385bc1de6a7a1b26db1dc541abef93"
$env:CONTENT_FFPROBE_EXPECTED_SHA256 = "55bb6c6289367ae2383efa86b26bf2596f8adb72ac747360eb13df162354161c"
$env:AUTOMATION_FOUNDRY_REHEARSAL_OUTPUT = "C:\temp\rehearsal"   # opcional
.venv\Scripts\python -m pytest tests/integration/test_local_media_rehearsal.py -s
```

Sem `AUTOMATION_FOUNDRY_REHEARSAL_OUTPUT` o ensaio publica em um diretório temporário do pytest e
imprime o caminho do MP4 e do relatório A6 para inspeção humana. Nenhuma variável habilita
publicação, upload, gasto ou acesso externo. Os defaults de `CONTENT_TTS_BACKEND`,
`CONTENT_VISUAL_BACKEND`, `CONTENT_CAPTION_BACKEND` e `CONTENT_ASSEMBLY_BACKEND` continuam
`disabled`; o ensaio monta sua própria projeção de configuração e não altera o `.env` local.

## Baseline observado neste checkpoint

O ambiente fornecido possui Python 3.12.13. A primeira coleta, antes da instalação das
dependências do projeto, produziu:

- `python -m pytest -q`: interrompido na coleta por ausência de `sqlalchemy` e `httpx`;
- `python -m ruff check .`: não executado porque o módulo `ruff` não estava instalado;
- `python -m mypy src`: não executado porque o módulo `mypy` não estava instalado.

Após criar `.venv` e instalar `.[dev]` sem os extras `ai`:

- `python -m pip install -e ".[dev]"`: concluído; a primeira tentativa foi bloqueada pela rede da
  sandbox e a repetição com acesso autorizado concluiu a instalação;
- `python -m pytest -q`: **530 passed, 12 skipped** após as três fatias deste checkpoint
  (**484 passed, 12 skipped** no checkpoint anterior); dez skips são integrações opt-in que exigem
  PostgreSQL/Redis descartáveis, FFmpeg/ffprobe fixados ou Kokoro, eSpeak NG, CUDA e escuta
  humana local, e dois são os testes de symlink do inventário de retenção, que exigem permissão
  de criação de link simbólico nesta máquina Windows;
- `python -m ruff check .`: encontrou 7 ocorrências mecânicas preexistentes; o autofix removeu
  imports não usados, ordenou imports e simplificou um context manager; a repetição terminou com
  **All checks passed**;
- `python -m mypy src`: **Success: no issues found in 76 source files**;
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
  de artifacts, schedules, experiments, métricas, alertas, ledger, controles e dispatch `requeued`
  concluídos; as vinte e duas tabelas compartilhadas, as oito tabelas do Content Engine e
  `alembic_version` foram criadas.
- PostgreSQL 17 descartável: todas as migrations, incluindo 0021/0022, e `alembic check` concluídos;
  duas runs em sessões concorrentes foram persistidas e o replay da mesma chave retornou a run
  original. Dois enables simultâneos do mesmo schedule produziram um único evento, e duas gravações
  simultâneas da mesma observação de connector produziram uma linha e um replay; dois disables
  administrativos concorrentes também produziram um único evento. O container e seus dados
  efêmeros foram removidos após o teste. A repetição usa `TEST_POSTGRESQL_URL` com
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
- Controle de schedule pelo dashboard: habilitar e desabilitar registraram exatamente os dois
  eventos auditados, replay da habilitação não duplicou evidência e o payload permaneceu redigido.
  Automação desabilitada, kill switch, executor ausente, CSRF inválido/Unicode e confirmação ausente
  recusaram a mutação sem alterar o schedule; o operador recebeu diagnóstico allowlisted e ainda
  pôde desabilitar sob kill switch.
- Health real com Redis 7 descartável: três workers simultâneos, um para cada fila `gpu`, `cpu` e
  `io`, foram contados sem expor seus nomes. O container e os resultados efêmeros foram removidos.
- Observabilidade de connectors no dashboard: três connectors e duas observações sucessivas da
  mesma chave provaram seleção latest-per-connector, expiração independente do status e dos dados,
  estados `fresh`, `stale` e `not_applicable`, score decimal exato no schema criado por Alembic e
  redaction de owner e chaves de idempotência. Timestamp futuro foi recusado. A home e
  `/data-observability/fragment` responderam `no-store`, mostraram UTC absoluto e declararam polling
  HTMX local a cada 30 s; uma consulta limitada sinalizou truncamento e preservou a prioridade de
  estado degradado. Nenhuma integração externa foi consultada.
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
- Backup SQLite isolado: a CLI criou um bundle real, publicou-o por rename atômico e verificou
  banco, configuração redigida, ZIP de artefatos e manifesto. Corrupção, path traversal, chave
  sensível, runtime ativo e falha parcial foram recusados; o caminho PostgreSQL foi validado com
  comandos mockados e sem senha na linha de comando.
- Gate A7 pelo dashboard: com o gate habilitado, a run gerada pelo ensaio parou em
  `awaiting_approval`, o detalhe da run exibiu a projeção segura com os dois SHA-256 e a frase de
  não publicação, o POST de aprovação com CSRF e confirmação concluiu a run como `succeeded` com
  `published=false`, e a página não ofereceu a revisão integral do roteiro para essa approval.
- Gate A8 pelo dashboard de teste: com A7 e A8 habilitados, aprovar A7 abriu uma terceira approval
  sem duplicar os seis steps. A página A8 serviu os dois PNGs A3 com `no-store`, persistiu exatamente
  o ID e SHA-256 escolhidos e concluiu a mesma run com `published=false`. ID forjado recebeu 400;
  adulteração do PNG fez página e mídia responderem 409, preservando a approval pendente e a run.
- Export local pelo dashboard: a mesma run A8 concluída expôs uma página `no-store`, manifesto
  JSON canônico, MP4 e PNG com bytes idênticos aos artifacts aprovados, sem caminho de storage no
  HTML ou JSON. Depois de adulterar o PNG selecionado, a reconstrução completa respondeu 409 e não
  serviu o pacote. Remover as três chaves A8 do output enquanto a approval A8 continuava no banco
  também respondeu 409; uma run A7-only legítima permaneceu exportável sem thumbnail. Quatro rotas
  de export fizeram quatro verificações A7, provando uma por requisição e nenhuma duplicação interna.
  Nenhum arquivo foi copiado e nenhum upload ou publicação foi executado.
- Resultado local por conteúdo: uma run recebeu métricas editoriais A1→A6, duas estimativas de
  energia, ledger BRL/USD e uma observação de outro módulo. O detalhe incluiu somente os nomes com
  contrato allowlisted, somou `0.0300000000 kWh`, preservou custos e receita líquida com dez casas
  decimais e excluiu a outra run. Source, category e dimensions não apareceram no resumo, que
  declarou explicitamente não ter consultado plataforma externa.
- Controles concorrentes no PostgreSQL 17 descartável: duas requisições de cancelamento carregadas
  antes da decisão produziram resultados `[changed=False, changed=True]` e um único `ControlEvent`.
  A corrida divergente da mesma chave de `ConnectorObservation` produziu uma linha e um conflito de
  domínio, sem `IntegrityError` bruto; o container e seus dados efêmeros foram removidos ao final.
- Página A7 servida por um dashboard real: um SQLite descartável recebeu uma run completa com MP4
  gerado pelo FFmpeg fixado e o dashboard local respondeu 200 em `/runs/1`,
  `/approvals/2/review` e `/approvals/2/final-video`, este último com `Content-Type: video/mp4` e
  330 847 bytes idênticos ao artifact. No navegador, o elemento `<video>` chegou a
  `readyState=4`, 1080×1920 e duração 12 s sem erro, e um seek para t=7 s desenhou um frame real em
  canvas, confirmando que a rota atende Range. Os decimais aparecem legíveis (`12 s`, `0.85`, `0`)
  sem perda de precisão. O processo efêmero e seus dados foram removidos após o teste.
- Ensaio local A1→A6 com FFmpeg real: com `AUTOMATION_FOUNDRY_TEST_FFMPEG=1` e os hashes fixados
  do build Gyan 8.0.1, a cadeia completa percorreu os seis steps `gpu, gpu, io, cpu, cpu, cpu` e
  terminou `succeeded`. A A3 importou dois PNGs sintéticos pelo manifesto, A4 distribuiu a narração
  aprovada sobre os 12 s reais do WAV e A5 produziu um MP4 de 330 847 bytes conferido por ffprobe
  como 1080×1920, uma track H.264 e uma AAC, duração 12,000000 s. Um frame extraído em t=7 s mostrou
  o segundo visual e a legenda ASS com karaoke e acentuação PT-BR corretas. O relatório A6 registrou
  `blocked=false` e `reference_count=0`. A raiz de importação permaneceu byte a byte inalterada e
  nenhum upload existe. O áudio é um tom sintético declarado, não narração.
- Kokoro A2 real na RTX 3090: após instalar `kokoro==0.9.4`, PyTorch `2.7.1+cu128` e eSpeak NG,
  o teste opt-in `AUTOMATION_FOUNDRY_TEST_KOKORO=1` passou em 79 s. CUDA 12.8 ficou disponível e
  o backend publicou somente um WAV temporário local de quality test, com pesos/revisão fixados;
  também foram geradas amostras curtas locais das três vozes PT-BR permitidas, todas PCM mono a
  24 kHz. Não há decisão de qualidade ou aprovação comercial: a proveniência das vozes continua
  não verificada e o backend permanece desabilitado por default.
- Expurgo aprovado real, ponta a ponta, em SQLite e raiz de storage descartáveis: com
  `RETENTION_PURGE_ENABLED=true`, dois artifacts foram registrados e apenas um teve a retenção
  vencida. `plan-purge` propôs 1 arquivo/29 bytes e abriu a approval sem apagar nada. `execute-purge`
  foi recusado antes da aprovação e com confirmação errada, sem criar backup nem tocar o disco.
  Depois de `decide-approval --approve`, dois testes de adulteração falharam como esperado: um com
  tamanho diferente e outro com **exatamente o mesmo tamanho** e conteúdo diferente, provando que a
  recusa vem do hash e não do tamanho. Restaurados os bytes aprovados, a execução removeu somente o
  arquivo expirado, preservou o retido, criou o bundle `20260811T211414Z-6d77ac15` e registrou
  `storage.purged_files=1` e `storage.purged_bytes=29`. O arquivo apagado foi recuperado do
  `artifacts.zip` do backup prévio com o SHA-256 original idêntico. Um segundo `execute-purge`
  retornou `replayed=true` sem apagar nada, e o inventário passou a mostrar `purged=1` e
  `retained=1`, com `missing=0`. Banco, storage e bundles efêmeros foram removidos após o teste.
- Inventário de retenção com CLI e dashboard reais: um SQLite descartável recebeu uma run de exemplo
  e um artifact registrado com 30 dias de retenção; a raiz de storage tinha ainda um arquivo sem
  registro e um bundle em `backups`. `storage-inventory --json` classificou 1 `retained` e 1
  `orphan`, contou 1 arquivo excluído e reportou `dry_run=true` com `deleted_file_count=0`. Forçando
  `retention_until` para o passado, o mesmo comando passou o item para `expired` sem tocar o disco:
  o conjunto de SHA-256 dos arquivos ficou byte a byte idêntico antes e depois. `--no-dry-run`
  retornou código 2 sem executar a varredura. O dashboard local respondeu 200 em `/storage` com
  `Cache-Control: no-store`, exibiu os totais e o slug da automação e não renderizou nenhum caminho
  de arquivo nem a raiz absoluta. O banco e a raiz efêmeros foram descartados após o teste.
- Backup/restore PostgreSQL 17 descartável: um projeto Compose e volume exclusivos receberam dados,
  executaram `pg_dump`, sofreram uma alteração e retornaram ao estado anterior por
  `pg_restore --single-transaction`; o backup automático pré-restore e o artefato também foram
  verificados. Container, rede e volume do teste foram removidos ao final.
- Runtime doméstico persistente: PostgreSQL 17 e Redis subiram presos a loopback, as vinte e duas
  migrations foram aplicadas e `alembic check` não encontrou operações novas. Dashboard, Beat e
  os workers `gpu`, `cpu` e `io` ficaram ativos; `doctor --services` aprovou oito checks e uma run
  `platform-smoke` enfileirada terminou com uma única run, um step e dispatch concluído. Quatro
  integrações marcadas para PostgreSQL/Redis passaram contra um banco temporário no mesmo serviço,
  depois removido. O supervisor foi parado cooperativamente para criar e verificar o backup
  PostgreSQL `20260822T145234Z-8dc8f737` em formato custom, com quatro artefatos, e voltou sem
  assumir ou parar os containers já ativos. O probe dos workers expôs um falso timeout específico
  do overhead do inspect no Windows; a margem externa foi ampliada de forma limitada, o probe
  voltou a contar os três workers e duas observações saudáveis resolveram o alerta persistido.

- Diagnóstico de alinhamento A4 em SQLite e storage descartáveis: a cadeia A1→A2→A4 rodou com o
  backend real `approved_text_timing_quality_test` sobre dois WAVs sintéticos distintos. Com fala
  contínua, o diagnóstico retornou `pass`, cobertura ≥ 0,95 e nenhum alerta; com um WAV cuja parte
  central é silêncio, o mesmo timing proporcional foi corretamente marcado `warning` com
  `CAPTIONS_OVER_SILENCE` e abriu exatamente um alerta local, que a repetição não duplicou. A
  segunda execução foi replay: mesmo artifact de relatório, mesmo digest e nenhuma métrica extra. A
  run permaneceu `succeeded` nos dois casos. Alterar os bytes do WAV registrado fez o diagnóstico
  responder `CAPTION_ALIGNMENT_EVIDENCE_UNVERIFIED` sem gravar relatório, métrica ou alerta;
  remover o `caption_ass` respondeu `CAPTION_ALIGNMENT_CAPTION_ARTIFACT_MISSING`; uma run de outra
  automação respondeu `CAPTION_ALIGNMENT_RUN_NOT_ELIGIBLE`. O relatório publicado não contém pauta,
  persona nem narração, e declara `published=false` e `external_service_consulted=false`. Nenhum
  download, chamada de rede, modelo externo ou publicação participou. A reexibição pelo dashboard
  também foi verificada: com o relatório íntegro, `/runs/<id>` respondeu `no-store` com status,
  motivo, desvio negativo preservado e digest dos parâmetros, sem o caminho relativo nem a raiz
  absoluta no HTML; depois de substituir o arquivo por `{}`, a mesma página respondeu 200 com o
  aviso seguro e sem nenhum número do relatório anterior. Conteúdo adulterado com hash
  recalculado — `schema_version`, `published`, `run_id` ou digest divergentes — também é recusado
  pelo contrato, não apenas pelo checksum.

- Política declarativa de retenção: a suíte prova que os oito tipos produzidos hoje resolvem 90
  dias — os mesmos valores anteriores, portanto nenhuma elegibilidade de expurgo mudou —, que um
  tipo não declarado resolve para retenção indefinida sem `retention_until`, e que
  `register-artifact` sem `--retention-days` grava 90 dias para `script_bundle` e retenção
  indefinida para um tipo desconhecido. Um teste de guarda varre `src/` e falharia se um produtor
  novo registrasse um `artifact_type` não declarado. `/storage` renderiza a declaração inclusive
  quando o inventário falha, e `retention-policy` é somente leitura. Nenhum arquivo foi criado,
  alterado ou apagado por esta fatia.

## Verificações bloqueadas ou adiadas

- **Computador de casa:** CPU, RAM e disco responderam ao snapshot. O boot completo real passou
  com `.env` PostgreSQL válido, volume persistente, Redis, dashboard, Beat e três workers; o
  lifecycle cooperativo em idle também passou. Ainda falta interromper um worker durante uma task
  real e confirmar o reclaim contra esse volume.
- **GPU:** RTX 3090 e VRAM responderam ao `nvidia-smi`; CUDA 12.8, PyTorch `2.7.1+cu128`, eSpeak
  NG e Kokoro 0.9.4 passaram no teste A2 curto com as três vozes PT-BR permitidas. Escuta humana,
  duração longa, carga prolongada e a decisão explícita sobre qualidade/proveniência comercial
  permanecem pendentes.
- **Mídia real do ensaio A1→A6:** o encadeamento foi executado com FFmpeg real, mas usando um WAV
  de tom sintético e PNGs sintéticos gerados pelo próprio projeto. Narração PT-BR real, pacote
  visual real com direitos revisados, sincronismo de legenda contra voz real, formato long e
  revisão editorial do vídeo final continuam pendentes.
- **Serviços locais:** Redis, PostgreSQL persistente, Ollama, dashboard, Beat e os três workers
  passaram juntos no runtime doméstico; `doctor --services` aprovou os oito checks. A recuperação
  após perda real do broker já passou em ambiente descartável, e o ciclo manual de queda e retorno
  do Ollama passou com falha segura e recuperação HTTP 200. O backup do volume PostgreSQL
  persistente foi criado e verificado; o restore desse volume, a perda real do Redis nesse runtime
  e a falta de disco em filesystem real continuam pendentes.
- **Timeouts de worker no Windows:** soft/hard limits estão configurados, mas o pool `solo` não
  oferece todas as garantias de timeout dos pools baseados em processos. O task wrapper mantém seu
  timeout assíncrono; preempção de código síncrono bloqueante continua pendente de validação local.
- **Credenciais:** validar somente em modo opt-in as APIs oficiais de Google/YouTube, Reddit,
  bancos de mídia, X e demais provedores. Nenhuma credencial deve entrar no Git.
- **Ações externas:** publicação, mensagens, gastos e ações financeiras continuam sem
  implementação. A exclusão material existe apenas no expurgo aprovado de artefatos expirados,
  desabilitado por default e verificado somente em SQLite descartável; ele não sai da máquina local. O gate editorial do bundle A1 pode autorizar somente a
  transformação TTS local do conteúdo exato; publicação e cada efeito externo futuro ainda exigem
  seus próprios gates específicos. O gate A7 aprova apenas o vídeo local já produzido e registra
  `published=false`; ele não autoriza upload nem cria promessa de publicação.
  O gate A8 registra somente a thumbnail local escolhida e também termina em `published=false`.

## Próxima fatia recomendada

O encadeamento A1→A6 já roda ponta a ponta com mídia local sintética e FFmpeg real, então a próxima
fatia é substituir a fixture de áudio pela narração real: executar a integração opt-in do Kokoro na
RTX 3090, escutar amostras das três vozes PT-BR e registrar qualidade, desempenho e a decisão
explícita sobre o risco residual de proveniência. Com o WAV real disponível, repetir o mesmo ensaio
`AUTOMATION_FOUNDRY_TEST_FFMPEG=1` trocando apenas o backend A2 passa a medir sincronismo de legenda
contra voz real e a expor a deriva que o timing proporcional de A4 não detecta. FLUX.1 permanece
bloqueado, e um pacote visual real ainda depende de assets revisados com direitos próprios. Depois
desse ensaio com narração real, o gate A7, a escolha A8 e o export local verificado já existem. A
agregação local de indicadores, energia, custo e resultado por conteúdo agora existe no detalhe da
run sem consultar plataformas externas. Fechar o item de analytics exige uma fonte oficial
pós-publicação, freshness e atribuição auditável; isso depende do contrato de upload/publicação e de
autorização explícita, então não foi presumido nesta fatia. O alinhamento auditado de legendas já
existe como diagnóstico somente leitura, e ele passa a ser a melhor medida objetiva do ensaio com
narração real: rodar `caption-alignment` sobre a run gerada pelo Kokoro mostra numericamente a
deriva que o timing proporcional produz contra voz real. O escopo por canal do A6 permanece
deliberadamente aberto porque não existe identidade de canal durável no banco; fechá-lo é decisão de
produto do proprietário, não uma fatia autônoma. Dentro do próprio tema de legendas, o passo
seguinte seria transformar o diagnóstico em gate opcional ou usá-lo para corrigir os timings, e
ambos exigem primeiro medir contra narração real. Na
Fase 5, o inventário de retenção e o expurgo aprovado já existem, então a próxima fatia de
confiabilidade agora tem reinício durante task, perda de Redis, falta de disco e indisponibilidade
do Ollama cobertos por simulações locais: o reclaim recupera tentativa `running` sem duplicar
trabalho, o broker preserva a entrega para reenvio, e A1 falha com código redigido sem persistir
artefato/approval parcial. Ainda falta repetir essas recuperações com PostgreSQL/Redis/worker e
Ollama reais. Dentro do próprio tema de storage, a política declarativa de retenção por tipo
de artefato passou a existir, então o que resta é o tratamento de órfãos — arquivos sem registro nem
hash conhecido, que por isso não podem entrar no mesmo plano verificado por SHA-256 e precisam de um
contrato próprio — e o expurgo agendado. Ambos ampliam a superfície de exclusão material e, por
isso, dependem de autorização explícita do proprietário antes de serem implementados; migrations
0019 e 0020 já passaram novamente em PostgreSQL descartável.
