# Arquitetura do Automation Foundry

**Versão:** 2.0  
**Status:** arquitetura-alvo para evolução incremental  
**Runtime:** local no computador principal do proprietário  

Consulte também `ROADMAP.md`, `AGENTS.md` e as skills em `.agents/skills/`.

## 1. Definição do produto

Automation Foundry é uma plataforma local para construir, executar e acompanhar automações ou semi-automações úteis, demonstráveis em portfólio e, quando validado, capazes de gerar ou economizar dinheiro.

Um único control plane oferece:

- disparo manual e agendado;
- estado de runs e steps;
- aprovações pendentes;
- retry, cancelamento e kill switch;
- artefatos e logs;
- métricas de qualidade, custo, receita e valor entregue;
- saúde do computador, workers e connectors.

Cada domínio continua isolado como módulo. Content Engine, Trading Lab e futuras automações compartilham infraestrutura e contratos, não suas regras de negócio.

## 2. Estado real da implementação

Na data desta versão:

- `src/core/config.py`, `database.py` e modelos SQLAlchemy formam a base inicial;
- `src/pipeline/script_gen.py` implementa a geração de roteiro do Content Engine e possui cobertura de testes relevante;
- o schema atual ainda é orientado a conteúdo;
- Celery possui topologia local, probes de infraestrutura e dispatch durável para executores registrados; grande parte dos módulos planejados permanece vazia ou incompleta, e o dashboard cobre visões iniciais e controles locais limitados;
- metadata, defaults locais e nomes de serviços usam a identidade `automation-foundry`;
- `src/cli.py` oferece o diagnóstico local `automation-foundry doctor` e inicia o dashboard com binding validado em loopback;
- `src/platform/` implementa parcialmente o kernel com `Automation`, `Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, `MetricPoint`, `Experiment`, `LedgerEntry`, `Alert` e históricos persistidos de transições;
- vinte e duas migrations Alembic incrementais cobrem as tabelas compartilhadas, o dispatch duravel, ocorrencias de schedule, estado de health, controles administrativos, continuacoes aprovadas, a evidencia de expurgo de artifacts, a escolha estruturada de approvals, observações de connectors e o baseline das oito tabelas legadas do Content Engine, mantendo `platform_alerts` separado de `alerts`;
- o caminho PostgreSQL local usa `asyncpg`, pool limitado, timeouts de conexão/comando e migrations validadas; SQLite permanece como bootstrap de processo único e backend dos testes;
- `src/core/celery_app.py` declara somente as filas `gpu`, `cpu` e `io`, usa Redis como broker e backend efêmero de probes e recusa criação implícita de filas; o entrypoint de worker exige PostgreSQL e serializa cada fila no Windows;
- `src/platform/dispatch_service.py` persiste a run e `delivery_id` antes da publicação, registra tentativas e falhas de broker, concede claims com lease e impede que entregas duplicadas executem novamente uma run terminal; quando uma approval libera novo trabalho na mesma run, o dispatch concluido pode voltar auditadamente a `pending` com evento `requeued`, preservando a mesma identidade e deixando o executor decidir a fase atual pelo banco;
- `automation-foundry run-example` executa um único passo `io` no-op, idempotente e com checkpoints transacionais, somente em processo local;
- `src/platform/executor_registry.py` permite disparo e retry somente para executores registrados em código; o smoke inline e o Content Engine A1 em background estao declarados, e retries elegiveis criam uma nova run ligada a falha retryable anterior com actor e motivo;
- `src/pipeline/a1_executor.py` adapta a geracao A1 existente ao contrato compartilhado: valida pauta, persona, formato e aberturas recentes, enfileira um step `gpu`, grava atomicamente um bundle JSON revisavel, registra artifact com retencao de 90 dias, metrica de tamanho e custo de API externa zero e move a run para `awaiting_approval`; a approval editorial fica ligada ao ID e SHA-256 do bundle; com TTS desabilitado a decisao encerra A1, e com TTS habilitado ela acorda a mesma run para A2 e para os gates/stages posteriores configurados;
- `src/pipeline/tts.py` implementa o contrato A2 independente do backend: exige a approval exata do bundle A1, executa na fila `gpu`, aceita somente WAV PCM S16LE mono validado, publica o arquivo por rename atomico e registra artifact, duracao, energia estimada, proveniencia/licenca declarada pelo adapter e custo externo zero; `disabled` permanece o default, e o unico backend real selecionavel e `kokoro_quality_test`, com pacote 0.9.4, revisao/peso/voz fixados, CUDA e eSpeak NG externo obrigatorios, chunking limitado e proveniencia marcada como nao aprovada para monetizacao; os demais testes usam adapter fake, e `pm_alex` passou por execuções integrais de 25,85 s e 39,325 s na RTX 3090, a segunda sob aprovação A2 persistida, sem resolver a proveniência comercial;
- `src/pipeline/narration_review.py` implementa o gate humano opcional entre A2 e A3. `CONTENT_NARRATION_REVIEW_ENABLED=true` exige TTS e continuação visual habilitados, reconfere bundle, step e WAV por tamanho, SHA-256 e metadados PCM, calcula a quantidade e as dimensões exatas exigidas por A3 e abre a approval `review_content_narration_a2`. O dashboard serve apenas o WAV verificado por uma rota local `no-store`/`nosniff`, sem renderizar seu caminho. Aprovar reabre o mesmo dispatch e os steps A1/A2 são replay idempotente antes de A3; rejeitar cancela, e qualquer adulteração impede a continuação. O gate não publica, envia nem realiza gastos;
- `src/pipeline/visuals.py` implementa o contrato A3 independente do backend: consome o bundle A1 aprovado e o WAV A2 verificados, calcula um visual a cada seis segundos com limite, exige proveniencia/licenca e direito comercial por asset, valida PNG completo nas dimensoes do formato, publica imagens e manifesto atomicamente e registra artifacts, contagem, cobertura, energia estimada e custo; A2 deixa a run aberta quando A3 esta habilitado, e somente A3 a conclui sem alterar o audio. `disabled` permanece o default; `local_assets_quality_test` importa na fila `io` somente PNGs selecionados pelo operador em uma raiz contida, com manifesto estrito, direitos explicitos e SHA-256 por item, sem download ou repeticao automatica. Isso habilita validacao local, mas nao substitui revisao humana nem prova juridica independente; FLUX.1 e provedores stock seguem bloqueados;
- `src/pipeline/captions.py` implementa o contrato A4 independente do backend: consome somente o WAV A2 verificado e a narracao ligada a approval A1, exige timestamps por palavra monotonicos e confinados a duracao, idioma PT, similaridade minima com o texto aprovado, proveniencia/licenca e direito comercial; publica ASS com karaoke por rename atomico e registra artifact, contagem, similaridade, energia estimada em GPU quando aplicavel e custo zero. `disabled` permanece o default. `approved_text_timing_quality_test` roda em `cpu`, distribui deterministicamente as palavras aprovadas pela duracao real do WAV e serve como fallback de teste. `faster_whisper_small_quality_test` roda em `gpu`, usa `faster-whisper==1.2.1`/`ctranslate2==4.8.1` e o modelo MIT `Systran/faster-whisper-small` na revisao `536b0662742c02347bc0e980a01041f333bce120`; o runtime exige snapshot local, confere tamanhos, Git blobs e SHA-256 dos pesos antes da carga e proibe download implicito. O teste opt-in com uma narracao `pm_alex` fresca passou na RTX 3090 e produziu ASS valido. Seus timestamps sao guiados pelo audio/ASR, nao constituem alinhamento forcado classico, e continuam sujeitos ao gate observavel e a revisao humana;
- `src/pipeline/caption_alignment.py` implementa o diagnóstico auditável do alinhamento A4. Ele é
  somente leitura: reconfere por tamanho e SHA-256 o `narration_audio` e o `caption_ass` da mesma
  run, mede em uma única passada a energia RMS do WAV por janelas de 20 ms, deriva os trechos de
  fala por limiar relativo ao pico com piso absoluto, compara-os com os eventos `Dialogue` do ASS e
  publica um relatório JSON canônico mais cinco métricas allowlisted atribuídas à run. Ele nunca
  transiciona a run, nunca regenera legenda, nunca cria step e nunca consulta serviço externo;
  quando cobertura, sobreposição de silêncio, desvio de início/fim ou eventos além do áudio saem da
  tolerância configurada, o resultado é `warning` com códigos fechados e um alerta local
  deduplicado. O relatório carrega o digest dos parâmetros e dos dois hashes, de modo que repetir o
  diagnóstico com a mesma evidência e a mesma configuração é replay exato, e mudar a configuração
  produz um relatório separado em vez de sobrescrever o anterior. Evidência adulterada, ASS
  inválido, WAV que não seja PCM S16LE mono e run de outra automação falham fechado antes de
  qualquer persistência. O detalhe da run reexibe o último relatório depois de reconferir tamanho e
  SHA-256 e de validar schema, algoritmo, run, status e motivos contra conjuntos fechados; um
  relatório ausente some da página, um relatório adulterado vira aviso seguro e nunca derruba o
  detalhe da run, e nenhum caminho de storage é renderizado. O diagnóstico mede sincronismo
  observável, não substitui revisão humana nem alinhamento forçado por modelo;
- `src/pipeline/caption_alignment_gate.py` transforma o mesmo diagnóstico em um gate A4 opcional,
  desabilitado por default e permitido somente com A4, A5 e A7 habilitados. Depois de A6 passar, o
  gate cria um step `cpu` ordinal 7 ligado aos IDs e SHA-256 do WAV e do ASS, publica ou reutiliza o
  relatório auditável e só permite criar A7 quando o status exato é `pass`. `warning`, evidência
  ausente/adulterada ou falha do diagnóstico encerram a run com código fechado e sem approval A7;
  o relatório e o alerta local permanecem para diagnóstico. Um `pass` congela step, artifact,
  SHA-256 e digest dos parâmetros no payload de A7, e A7/A8 reconferem essa cadeia antes de concluir;
- `src/pipeline/assembly.py` implementa o contrato A5 independente do backend: exige WAV, manifesto/imagens A3 e ASS A4 verificados por tamanho e SHA-256, reconcilia o manifesto com cada imagem e monta na fila `cpu`; o resultado precisa declarar H.264, AAC, legendas queimadas, licença e direito comercial, e o validador inspeciona boxes MP4, tracks, resolução e duração antes do rename atomico. Artifact, duração, tempo de render, bytes, energia CPU estimada e custo zero ficam observáveis. `disabled` permanece o default; `ffmpeg_quality_test` aceita somente o build externo Gyan 8.0.1/WinGet fixado por hashes, usa subprocess sem shell e confirma tracks/duração com ffprobe real. Além da amostra short sintética reproduzível, a run 4 montou narração real, sete visuais e legenda em um MP4 1080×1920 de 39,325 s aprovado em A7; formato long e política de redistribuição continuam pendentes;
- `src/intelligence/similarity.py` implementa o primeiro contrato A6 sem modelo externo: verifica o roteiro aprovado e o MP4 A5, compara unigramas por cosseno e trigramas por Jaccard contra uma janela configurável de runs anteriores da mesma automação que já passaram pelo gate, e registra relatório JSON, métricas, energia CPU estimada e custo externo zero. Score máximo igual ou superior ao limite abre alerta local e falha a run com bloqueio não retryable; nenhum upload existe. O escopo continua sendo a automação: não existe identidade de canal durável e validada no servidor para restringir a janela, porque `channels` é uma tabela legada sem nenhum caminho de criação, sem vínculo com `Run` e ligada a credenciais de publicação fora do escopo atual; criar esse contrato é decisão de produto e não foi fabricado aqui. A comparação lexical também não substitui uma futura análise semântica auditada;
- `src/pipeline/final_review.py` implementa o gate humano A7 sobre o vídeo final. Ele é desabilitado por padrão; habilitado, A6 deixa de concluir a run e A7 confere MP4 e relatório de originalidade por tamanho e SHA-256, exige que o relatório descreva exatamente aquele vídeo e não esteja bloqueado e, quando o gate de alinhamento existe, reconfere também seu step e relatório `pass`. A approval fica ligada ao digest dessas evidências, com projeção segura de resolução, duração, bytes, codecs, hashes, originalidade e sincronismo. Aprovar conclui a run local com `published=false` quando A8 está desabilitado, ou abre A8 na mesma run quando ambos os gates estão habilitados; rejeitar cancela pelo contrato compartilhado; qualquer alteração posterior recusa a decisão sem transicionar a run. Nenhum upload, envio ou gasto existe, e o gate não autoriza publicação futura. O dashboard oferece a página `no-store` dedicada da approval e transmite o MP4 exato por uma rota própria, com `nosniff`, sem cache e com Range, sempre reconferindo hash e digest antes de servir; approval de outra ação recebe 404 e evidência adulterada recebe 409;
- `src/pipeline/thumbnail_review.py` implementa o gate humano A8, também desabilitado por padrão e dependente de A7. Depois de A7 aprovado, ele congela no digest da approval somente os artifacts `visual_image` PNG do step A3 da mesma run, reconferindo confinamento em `STORAGE_ROOT`, tamanho, assinatura PNG e SHA-256. A página `no-store` serve cada candidato localmente apenas após reverificar o conjunto completo, e o formulário exige exatamente um ID validado no servidor. A decisão imutável persiste `thumbnail_artifact_id` e `thumbnail_sha256`; aprovar conclui a mesma run com as evidências A7/A8 e `published=false`, enquanto rejeitar cancela. ID arbitrário ou de outra run, arquivo adulterado, digest ou payload divergente e replay não concluem a run nem criam upload;
- `src/pipeline/local_export.py` reconstrói um export local somente para runs `succeeded` com
  `published=false`: exige a approval A7 aprovada e íntegra, valida a approval/decisão A8 quando
  presente e produz em memória um manifesto JSON canônico ligado aos IDs, tamanhos e SHA-256 do
  MP4 e do PNG selecionado. O dashboard oferece uma página `no-store` e downloads locais com
  `nosniff`, sempre repetindo a verificação completa antes de servir. Se uma approval A8 existir,
  omitir suas chaves do output falha fechado; a evidência A7 já verificada é reutilizada dentro da
  mesma requisição para não hashear o MP4 duas vezes. Nenhum caminho de storage, cópia, ZIP, upload
  ou publicação é criado;
- o encadeamento A1→A6 possui um ensaio local reproduzível: `tests/support/local_media_rehearsal.py` gera dentro do repositório um WAV determinístico de tom puro declarado como `synthetic_quality_test_fixture`, PNGs sintéticos com manifesto A3 estrito e um roteiro PT-BR fixo, sem download, rede, API externa ou asset de terceiro. A suíte padrão exercita os backends reais de A3 e A4 na cadeia compartilhada, com A5 fake e A6 real, roda o diagnóstico de alinhamento A4 sobre a evidência produzida e prova o bloqueio fail-closed quando o manifesto diverge da quantidade calculada; a variante opt-in `AUTOMATION_FOUNDRY_TEST_FFMPEG=1` repete a cadeia com FFmpeg fixado e publica um MP4 inspecionável. O ensaio não valida narração real, direitos sobre imagens reais, sincronismo contra voz real nem adequação editorial, e nada é publicado;
- uma validação manual opt-in complementar percorreu A1→A8 na run 4 com `pm_alex`, gate A2 persistido, sete PNGs 1080×1920 declarados em manifesto, timing A4 proporcional, FFmpeg real, bloqueio A6 e approvals A7/A8. O MP4 H.264/AAC de 39,325 s e a thumbnail escolhida foram servidos pelo export local após nova verificação de hashes; o diagnóstico de alinhamento retornou `pass`, e a run terminou `published=false`. Essa evidência valida o fluxo short e os gates, mas não transforma o adapter de voz em comercialmente aprovado, não substitui revisão jurídica dos assets, não valida formato long e não implementa alinhamento forçado nem publicação;
- `src/platform/task_runner.py` aplica timeout assíncrono, retries limitados e backoff exponencial; pedidos de cancelamento e kill switch são consultados antes e depois de cada corrotina e entre tentativas;
- `control_events` mantem auditoria append-only de pedidos de cancelamento, habilitacao administrativa e mudancas do kill switch por automacao; cada mudanca administrativa ou operacional registra o actor local e o motivo informado pelo operador, e os controles por automação usam lock de linha no PostgreSQL para não duplicar evidência sob requisições concorrentes;
- approvals vinculam run, ação e digest do payload; uma decisão pode ainda persistir uma escolha estruturada pequena, não sensível e imutável, e uma decisão aprovada só pode autorizar uma chave idempotente de task;
- artifacts vinculam arquivo local a run e, opcionalmente, step; o registro persiste somente caminho relativo e metadados após validar confinamento em `STORAGE_ROOT`, tamanho e SHA-256;
- schedules nascem desabilitados, validam cron POSIX de cinco campos e timezone IANA, calculam `next_run_at` em UTC e auditam criação, habilitação, desabilitação e cada ocorrência publicada ou ignorada; habilitação pela UI exige executor registrado e a mudança de estado bloqueia a linha no PostgreSQL para que requisições concorrentes produzam no máximo um evento;
- metric points são observações decimais append-only, idempotentes e atribuíveis a automation, run e step; unidade, fonte, confiança opcional e timestamp permanecem explícitos;
- experiments registram hipótese, variantes e métrica primária com lifecycle auditado; somente experiments em execução podem receber novas runs atribuídas, e hipóteses e motivos permanecem redigidos no dashboard;
- ledger entries são observações financeiras decimais append-only, idempotentes e atribuíveis a automation e, opcionalmente, run, step e metric point; tipo, categoria, moeda, fonte, confiança opcional e timestamp permanecem explícitos, sem executar pagamentos ou criar promessas;
- platform alerts deduplicam ocorrências, escalam severidade enquanto abertos e auditam reconhecimento, resolução e reabertura; nenhuma notificação externa foi implementada;
- `src/dashboard/` renderiza com FastAPI e Jinja uma visao geral `no-store`, schedules redigidos e detalhe de run com evidencias vinculadas; schedules expoem estado persistido e efetivo, cron, timezone, proxima execucao, ultima ocorrencia e somente diagnósticos allowlisted, approvals pendentes exibem idade calculada no servidor, o ledger possui totais globais e por automacao separados por moeda e cada automacao mostra taxa de sucesso e duracao media sobre ate 50 resultados succeeded/failed recentes, nunca payload, categoria ou fonte internos; para runs do Content Engine, o detalhe consolida somente métricas editoriais allowlisted — inclusive os indicadores do diagnóstico de alinhamento A4, que aparecem no escopo da run e preservam sinal negativo —, energia estimada e custo, receita, valor atribuído e receita líquida observados por moeda, sempre restritos à mesma run e sem alegar analytics de plataforma externa; o detalhe de run e a página de saúde também respondem `no-store`, e o detalhe usa HTMX vendorizado localmente para atualizar apenas status e steps enquanto a run nao e terminal, reutilizando a mesma projecao redigida; habilitacao administrativa, controle idempotente de schedule, disparo registrado, retry elegivel, kill switch, cancelamento, rejeicao e aprovacao usam POST com confirmacao e token CSRF por processo, e a comparação do token aceita entrada Unicode arbitrária sem gerar erro interno; cancelamentos concorrentes relêem e travam a run no PostgreSQL para registrar no máximo um evento; habilitar um schedule exige no servidor automacao ativa, kill switch livre e executor registrado, recalcula `next_run_at` e audita a transicao sem expor o payload; bloqueios operacionais retornam ao dashboard com mensagem segura, enquanto desabilitar continua possível; desabilitar uma automacao bloqueia novas runs e o inicio das enfileiradas sem interromper runs ativas, enquanto o kill switch preserva a semantica operacional; aprovacao positiva exige uma projecao limitada, validada e explicitamente segura do payload, e registros legados sem essa projecao permanecem bloqueados; A1 oferece o roteiro integral, A2 reproduz o WAV verificado e informa a quantidade/dimensões exatas de A3, A7 reproduz o MP4 verificado e A8 mostra somente os PNGs A3 congelados e exige uma escolha estruturada; runs A7/A8 concluídas oferecem manifesto e downloads locais verificados sem revelar caminhos ou publicar; quando o dominio declara continuacao habilitada, a mesma transacao reabre o dispatch e a publicacao posterior permanece recuperavel se o broker falhar;
- `src/operations/health.py` coleta um snapshot somente leitura e redigido de CPU, memoria, disco, GPU/VRAM, banco, Redis, Beat e workers por fila; Redis indisponivel impede apenas a inspecao efemera dos workers e nunca substitui o estado duravel no banco;
- `src/operations/health_alerts.py` mantem contadores consecutivos no banco; duas degradacoes abrem ou atualizam um alerta deduplicado e duas recuperacoes o resolvem, enquanto `skip` permanece inconclusivo; a task periodica roda na fila `io` e nao envia notificacoes externas;
- `src/operations/retention.py` produz um inventario somente leitura de `STORAGE_ROOT`: percorre a
  raiz sem seguir links, reconcilia cada arquivo com os artifacts registrados e classifica em
  `retained`, `retention_hold`, `expired`, `orphan`, `missing` e `unsafe`, com contagem e bytes por
  classe e por automacao. Runs abertas, approvals pendentes, tamanho divergente do registro e
  caminho compartilhado por mais de um registro impedem a classificacao `expired`; caminho
  registrado fora da raiz e symlink viram `unsafe` sem serem seguidos, e uma varredura acima do
  limite configurado falha em vez de entregar resultado parcial. `backups`, `runtime` e `.gitkeep`
  ficam fora da reconciliacao e aparecem apenas como total excluido. Nada e criado, alterado ou
  apagado: `storage-inventory` e dry-run por default, recusa `--no-dry-run` e a pagina `/storage`
  mostra somente totais redigidos, sem caminho de arquivo;
- `src/operations/retention_policy.py` declara, em um único lugar validado, a retenção de cada tipo
  de artifact produzido pelo projeto, com produtor e justificativa explícitos. Os produtores A1→A6 e
  o diagnóstico de alinhamento resolvem sua retenção dessa tabela em vez de constantes duplicadas, e
  `register-artifact` usa o mesmo default quando o operador não informa `--retention-days`. Um tipo
  não declarado resolve para retenção indefinida: ele nunca expira e por isso nunca entra em um
  plano de expurgo por omissão. A política vale somente para registros novos, porque
  `retention_until` é congelado na linha do artifact no momento do registro; alterar a tabela nunca
  antecipa o vencimento de algo já registrado. `automation-foundry retention-policy` e a página
  `/storage` mostram a declaração completa, e um teste de guarda recusa qualquer `artifact_type`
  produzido em `src/` que não esteja declarado;
- `src/operations/purge.py` e o unico caminho do projeto que apaga dados do operador. Ele fica
  desabilitado por default e se divide em dois atos deliberados. O planejamento converte os itens
  `expired` do inventario em uma lista imutavel verificada por SHA-256, persiste o plano na propria
  run e abre uma approval humana ligada ao digest exato dessa lista; a projecao revisada traz
  contagem, bytes, automacoes, tipos, sensibilidade e a frase de irreversibilidade, nunca um
  caminho. A execucao exige approval aprovada, runtime parado, confirmacao `PURGE <plan-digest>` e
  um backup verificado criado imediatamente antes; ela reconfere o SHA-256 de cada arquivo em uma
  varredura completa que aborta tudo antes da primeira remocao e novamente antes de cada `unlink`.
  Artefatos removidos preservam seu registro e recebem `purged_at` e `purged_by_approval_id`, de
  modo que o inventario os mostra como `purged` e nunca como `missing`; um arquivo que reaparece no
  caminho purgado vira `unsafe`. Falha depois da primeira remocao grava a evidencia parcial, deixa a
  run `failed` e mantem o backup previo como recuperacao. Rejeitar cancela a run sem apagar nada;
- `src/operations/backup.py` cria e restaura bundles atomicos e verificaveis com snapshot SQLite ou PostgreSQL, configuracao allowlisted sem secrets, artefatos e manifesto SHA-256; restore exige confirmacao exata, runtime parado, storage de artefatos vazio e um backup automatico do estado anterior;
- `src/platform/connector_service.py` registra observações append-only e idempotentes de connectors. Cada registro usa uma chave segura, status allowlisted, SLO do próprio check, último sucesso, SLO de freshness e qualidade normalizada opcional; `observed_at` não pode ultrapassar o relógio de gravação, e SQLite persiste o score como texto decimal canônico enquanto PostgreSQL usa `NUMERIC(5,4)`. Endpoint, credencial, payload e mensagem de erro não fazem parte do schema. Corridas da mesma chave usam savepoint e retornam replay ou conflito de domínio. O dashboard seleciona somente a observação mais recente por automação/connector, prioriza estados indisponíveis/degradados, sinaliza truncamento, mostra UTC absoluto e recalcula `stale` a partir do relógio do servidor, sem consultar a integração externa. O histórico continua append-only e ainda não possui uma política de retenção;
- `src/runtime/` e `src/runtime_cli.py` implementam um supervisor Windows singleton em foreground para PostgreSQL, Redis, os tres workers, Beat e dashboard; processos host usam eventos nomeados para shutdown cooperativo e um Job Object kill-on-close como fallback, enquanto o Compose para somente servicos iniciados pelo proprio supervisor e nunca remove volumes;
- `src/briefs/` implementa o módulo `operations-brief`, o segundo da plataforma e o primeiro sem relação com mídia. `evidence.py` coleta, somente leitura, contagens, estados, durações, idades, códigos de erro permitidos por regex e totais decimais do ledger de uma janela de 1, 7, 14 ou 30 dias; nunca inclui payloads, mensagens de erro, textos de revisão, `source`, `category` ou `dimensions`, e exclui a própria run. `render.py` é uma função pura que transforma essa evidência em Markdown, de modo que o mesmo JSON sempre gera o mesmo texto. `executor.py` roda um step `io`, publica evidência e brief por rename atômico, registra os artifacts, três métricas, custo externo zero e uma `ConnectorObservation` para `platform-database`, e abre a approval `review_operations_brief` ligada por digest aos IDs e SHA-256 dos dois arquivos. A página de revisão reconfere tamanho e hash, exige que o Markdown seja exatamente o renderizado a partir da evidência e só então mostra o brief; aprovar conclui a run localmente, rejeitar a cancela e nenhum efeito externo existe. Falha de escrita é `BRIEF_STORAGE_WRITE_FAILED` e falha ao persistir evidência é `BRIEF_EVIDENCE_PERSIST_FAILED`, ambas retryable por uma nova run. Schedule e narrativa por LLM ainda não existem;
- `src/platform/schedule_alerts.py` transforma uma ocorrência de schedule pulada em evidência visível: o dispatcher abre um alerta local deduplicado por schedule, com severidade pelo motivo (pausa do operador: `info`; sobreposição e tolerância excedida: `warning`; entrada inválida ou executor ausente: `error`), usando somente textos permitidos e nunca o payload. A próxima ocorrência preparada normalmente o resolve e uma nova ocorrência pulada o reabre. A gravação roda em savepoint e uma falha do alerta é registrada em log sem impedir o tick. O alerta só nasce quando o tick roda: se o runtime ficar parado e nunca voltar, nada é avisado, e nesse caso o sinal continua sendo a saúde do Beat e o schedule vencido no dashboard;
- o runtime completo não foi validado no computador de casa.

O roadmap deve evoluir essa base sem confundir placeholders com funcionalidades prontas e sem reescrever a parte testada apenas por estética arquitetural.

## 3. Objetivos e não objetivos

### Objetivos

- Operar localmente com configuração reproduzível.
- Adicionar novos módulos por um contrato consistente.
- Tornar estado, falhas, pendências e resultados visíveis.
- Medir custo e valor por automação e experimento.
- Usar a RTX 3090 com fila serializada e modelos locais quando fizer sentido.
- Manter decisões irreversíveis ou externas sob controle humano.
- Produzir evidências de engenharia úteis para currículo e portfólio.

### Fora do escopo inicial

- Serviço público, SaaS ou acesso remoto pela internet.
- Multi-tenancy, Kubernetes e microserviços.
- Escala distribuída em vários computadores.
- Promessas de lucro ou execução autônoma irrestrita.
- Trading com dinheiro real.
- Publicação, outreach ou gasto sem gate humano.

## 4. Visão do sistema

```text
┌──────────────────────────────────────────────────────────────┐
│ Control plane local — FastAPI + Jinja/HTMX                  │
│ runs · steps · aprovações · métricas · custos · saúde       │
└──────────────────────────┬───────────────────────────────────┘
                           │ comandos e consultas
┌──────────────────────────▼───────────────────────────────────┐
│ Plataforma compartilhada                                    │
│ contratos · scheduler · task wrappers · adapters · alerts   │
└──────────────┬──────────────────────┬────────────────────────┘
               │                      │
       ┌───────▼────────┐     ┌──────▼───────────────────────┐
       │ Redis + Celery │     │ PostgreSQL                  │
       │ gpu · cpu · io │     │ fonte durável da verdade   │
       └───────┬────────┘     └──────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│ Módulos de domínio                                           │
│ Content Engine · Trading Lab · futuras automações           │
└──────────────┬───────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│ Adapters locais e externos                                   │
│ Ollama · filesystem · APIs oficiais · feeds · paper broker  │
└──────────────────────────────────────────────────────────────┘
```

Codex, Cursor, Claude, plugins e MCPs são ferramentas de desenvolvimento. Nenhum deles faz parte do caminho obrigatório de execução do produto.

## 5. Estilo arquitetural

Use um monólito modular. Ele oferece fronteiras claras sem o custo operacional de microserviços em um sistema de uma pessoa e um computador.

Estrutura-alvo, migrada gradualmente:

```text
src/
├── platform/
│   ├── config/
│   ├── database/
│   ├── orchestration/
│   ├── observability/
│   ├── approvals/
│   └── adapters/
├── modules/
│   ├── content_engine/
│   ├── trading_lab/
│   └── <future_module>/
└── dashboard/
    ├── routers/
    ├── services/
    └── templates/
tests/
├── platform/
└── modules/
```

A estrutura atual em `src/core`, `src/pipeline` e pastas relacionadas pode permanecer enquanto cada migração tiver testes e benefício concreto. Evite uma movimentação em massa antes de obter um baseline verde.

## 6. Contrato compartilhado

Todo módulo declara:

- `slug`, nome e estado habilitado/pausado;
- schema de entrada e de saída;
- trigger manual, agendado, evento ou API;
- steps ordenados e fila de cada step;
- chave de idempotência;
- retry, timeout, cancelamento e recuperação;
- gates de aprovação e kill switch;
- artefatos e política de retenção;
- secrets, connectors e rate limits necessários;
- health checks e comportamento degradado;
- métricas operacionais, financeiras, de qualidade e de negócio.

O contrato não obriga módulos a compartilhar tabelas de domínio.

## 7. Modelo de estado

Conceitos genéricos da plataforma:

- `Automation`: registro e configuração do módulo.
- `Run`: uma execução iniciada por um trigger.
- `StepRun`: execução observável de uma etapa.
- `Approval`: decisão humana requerida.
- `Artifact`: arquivo ou resultado produzido.
- `Schedule`: agenda persistida.
- `MetricPoint`: observação temporal.
- `ConnectorObservation`: estado redigido, freshness e qualidade de uma integração.
- `Experiment`: hipótese e variante sob avaliação.
- `LedgerEntry`: custo, receita ou valor atribuível.
- `Alert`: condição operacional ou de risco.

Estados mínimos de uma run ou step:

```text
queued → running → succeeded
                 ↘ failed → queued (retry limitado)
queued/running → cancelled
running → awaiting_approval → running (approved) / cancelled (rejected ou cancelamento)
```

Toda transição é persistida no banco com timestamps, tentativa, erro estruturado e relação com a execução anterior quando houver retry. Decisões de approval são imutáveis, auditadas e vinculadas ao digest do payload protegido. O `actor` informado pela CLI é evidência operacional local, não autenticação forte.

## 8. Execução e agendamento

Celery executa as tarefas e Redis atua como broker e cache. Redis não substitui a persistência.

- `gpu`: Ollama pesado, TTS, imagem, embeddings ou transcrição; concorrência 1.
- `cpu`: FFmpeg, transformação de arquivos e cálculo paralelo.
- `io`: APIs, scraping permitido, downloads, uploads e coleta de dados.

Tasks devem ser idempotentes, reconhecer cancelamento, usar limites de tempo e produzir erros acionáveis. O dispatch prepara estado no PostgreSQL antes de publicar, usa identidade estável, histórico append-only e claim com lease; uma mensagem concorrente aguarda a lease e uma mensagem terminal não repete trabalho. O wrapper consulta controles persistidos antes e depois de cada corrotina e entre retries, mas não preempta código síncrono bloqueante. Um tick periódico do Beat consulta schedules no banco; cada horário previsto vira uma ocorrência única, com misfire, sobreposição e falha de publicação observáveis. Beat roda por entrypoint separado, com PID file do Celery e named mutex adicional no Windows. n8n só entra se integrações entre aplicações justificarem outra camada operacional.

No Windows, `automation-foundry-runtime start` e o caminho operacional canonico. O processo permanece visivel em foreground, executa preflight e migrations antes de abrir processos host ocultos, registra estado local com identidade PID protegida contra reutilizacao e encerra o conjunto ao receber `stop`, Ctrl+C ou uma falha de filho. PostgreSQL e Redis que ja estavam ativos nao sao reivindicados nem parados pelo supervisor.

## 9. Control plane

A arquitetura do dashboard usa FastAPI, Jinja e HTMX e escuta apenas em `127.0.0.1` por padrao. A home, o detalhe de run, saúde e os fragmentos operacionais respondem `no-store`. O detalhe de run carrega HTMX 2.0.10 vendorizado no pacote, sem CDN, e consulta um fragmento a cada dois segundos somente enquanto a run nao e terminal. O fragmento reutiliza a mesma projecao redigida da pagina completa e substitui apenas status e steps. A home usa o mesmo asset local para atualizar a cada trinta segundos o fragmento de connectors, freshness e qualidade; o servidor seleciona a observação persistida mais recente, deixa estados vencerem para `stale`, prioriza indisponíveis/degradados, sinaliza listas limitadas e mostra o horário UTC absoluto sem tocar na integração. A fila liga approvals editoriais com evidência integral às suas páginas locais `no-store`; nelas, aprovação e rejeição usam formulários HTML nativos com POST seguido de redirect, confirmação e CSRF, e uma decisão já registrada torna a página somente leitura. Uma run Content Engine concluída por A7/A8 oferece uma página separada de export local, manifesto e downloads que reconferem toda a evidência antes de servir. O controle de schedule usa o mesmo padrão nativo e auditado: apenas habilita ou desabilita futuros disparos locais, recusa habilitação quando a automação está pausada, o kill switch está ativo ou não há executor registrado, e não renderiza o payload. Um bloqueio esperado retorna por redirect com aviso seguro; estado efetivo e causas allowlisted permanecem diagnosticáveis sem JavaScript. Os demais formulários e controles seguem o mesmo padrão.

Visões mínimas:

- resumo de automações, resultado e alertas;
- runs atuais, próximas e recentes;
- steps, tentativas, duração e erro;
- fila de aprovações e idade da pendência;
- schedules e estado dos connectors;
- custo, receita e resultado líquido quando atribuível;
- CPU, RAM, GPU, VRAM, disco, Redis, banco e workers;
- qualidade e freshness dos dados.

Ações mínimas:

- iniciar uma run manual;
- cancelar ou pausar com segurança;
- retentar uma etapa elegível;
- aprovar ou rejeitar uma ação;
- habilitar ou desabilitar schedule e módulo;
- acionar kill switch.

## 10. Dados e artefatos

PostgreSQL é o alvo para execução concorrente. SQLite continua útil para testes e protótipos de processo único.

Arquivos grandes usam filesystem local com caminhos registrados no banco. A organização recomendada é:

```text
storage/<automation_slug>/<run_id>/<artifact_type>/...
```

Cada artefato registra checksum, tamanho, tipo, origem, data, retenção e sensibilidade. A retenção vem de uma política declarativa por tipo de artifact, com produtor e justificativa explícitos; tipo não declarado fica com retenção indefinida, e o prazo é congelado na linha no momento do registro. O registro atual inspeciona arquivos já existentes, rejeita caminhos que resolvam fora de `STORAGE_ROOT` e não cria, copia, altera nem apaga o conteúdo. O inventário de retenção reconcilia disco e registros e classifica cada item sem apagar nada. A exclusão material existe apenas no caminho de expurgo aprovado: desabilitada por default, ligada por SHA-256 à lista exata que um humano aprovou, com runtime parado, backup verificado prévio e reconferência de hash imediatamente antes de cada remoção. O registro do artifact sobrevive à remoção como evidência auditável. O backup trata banco, configuração sem secrets e artefatos como um bundle versionado: publica somente depois de verificar hashes e formatos, exige o runtime parado e nunca arquiva logs, estado operacional ou backups anteriores. O restore exige `RESTORE <backup-id>`, compatibilidade do banco, storage de artefatos vazio e cria um bundle automático do estado anterior; PostgreSQL usa `pg_restore --single-transaction`, e artefatos novos são removidos se o banco falhar. SHA-256 protege contra corrupção acidental, não substitui assinatura contra adulteração maliciosa.

## 11. Adapters e connectors

Integrações de produto ficam atrás de adapters próprios:

- modelo local por API do Ollama;
- APIs oficiais de plataformas e provedores;
- feeds de mercado e paper broker;
- notificações e exports;
- filesystem e ferramentas de mídia.

Plugins e MCPs podem ajudar agentes a desenvolver ou investigar, mas não substituem esses adapters. Todo adapter externo implementa timeout, retry limitado, rate limit, freshness, redaction de logs e modo fake para testes.

## 12. Segurança e aprovações

Exija aprovação humana antes de:

- publicar conteúdo ou responder comentários;
- enviar outreach ou mensagens externas;
- gastar dinheiro ou consumir um serviço pago fora do limite aprovado;
- apagar dados ou artefatos materiais;
- executar qualquer ação financeira.

Cada módulo com efeitos externos possui kill switch. Secrets permanecem fora do Git e recebem o menor escopo possível. O dashboard local não deve ser exposto publicamente por conveniência.

Trading Lab nunca permite que o LLM envie ordens. O caminho inicial termina em paper broker; qualquer proposta futura de dinheiro real exige decisão de arquitetura, threat model, limites, reconciliação e autorização explícita separada.

## 13. IA e hardware local

O hardware esperado é Ryzen 9 5950X, RTX 3090 com 24 GB de VRAM e aproximadamente 32 GB de RAM, ainda a confirmar no computador de casa.

- mantenha somente um workload pesado por vez na GPU;
- carregue e descarregue modelos de forma previsível;
- registre modelo, versão, parâmetros, duração e uso estimado;
- ofereça caminhos mockados ou CPU-light para testes;
- não afirme compatibilidade ou desempenho sem executar no hardware real.

## 14. Observabilidade e economia

Uma automação deve provar mais que execução técnica. Registre:

- taxa de sucesso, duração e retries;
- idade de pendências e tempo humano economizado;
- qualidade específica do domínio;
- consumo de GPU/CPU e custos externos;
- receita ou valor atribuído com fonte e grau de confiança;
- resultado líquido por módulo e experimento quando mensurável.

Métricas financeiras são observações, não promessas. Trading usa métricas de risco e validação fora da amostra, nunca apenas retorno bruto.

## 15. Módulos iniciais

### Content Engine

Primeiro módulo existente. Evolui de geração de roteiro para pipeline editorial com TTS, visuais, legendas, montagem, similaridade, aprovação, upload privado e analytics. Preserve licenças comerciais, variação real e revisão humana.

### Trading Research Lab

Ambiente isolado para ingestão de dados, pesquisa reproduzível, backtests sem vieses óbvios, walk-forward e paper trading. Dinheiro real permanece fora do escopo.

### Próximos módulos candidatos

- monitor de oportunidades e preços;
- inteligência de mercado e concorrentes;
- pesquisa e qualificação de leads com rascunhos aprováveis;
- geração de relatórios e briefs;
- automações pessoais de alto valor mensurável.

Cada candidato passa pelo contrato de `$build-automation-module` e por avaliação de valor, risco, custo de manutenção e qualidade dos dados.

## 16. Decisões arquiteturais vigentes

1. Local-first, sem deployment público inicial.
2. Monólito modular antes de microserviços.
3. FastAPI + HTMX antes de SPA.
4. PostgreSQL para concorrência; SQLite para testes e bootstrap.
5. Celery + Redis + Beat antes de n8n.
6. Banco como fonte de verdade e filesystem para artefatos.
7. GPU serializada.
8. Adapters da aplicação, não MCPs, no runtime.
9. Gates humanos para efeitos externos ou irreversíveis.
10. Backtest e paper trading antes de qualquer discussão de live trading.

## 17. Definição arquitetural de pronto

Uma fatia está pronta quando:

- o contrato e os estados são explícitos;
- o resultado é observável no control plane;
- retry, duplicação, falha parcial e cancelamento têm comportamento seguro;
- gates e kill switch funcionam quando aplicáveis;
- testes relevantes passam;
- secrets e dados sensíveis estão protegidos;
- métricas e custos essenciais são registrados;
- documentação corresponde à implementação;
- verificações dependentes do computador de casa estão executadas ou claramente pendentes.

## 18. Referência histórica

A especificação detalhada original do Content Engine foi preservada em `docs/reference/content-engine-v1.md`. Ela pode orientar o módulo, mas não substitui esta arquitetura nem o roadmap atual.
