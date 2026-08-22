# Arquitetura do Automation Foundry

**VersÃ£o:** 2.0  
**Status:** arquitetura-alvo para evoluÃ§Ã£o incremental  
**Runtime:** local no computador principal do proprietÃ¡rio  

Consulte tambÃ©m `ROADMAP.md`, `AGENTS.md` e as skills em `.agents/skills/`.

## 1. DefiniÃ§Ã£o do produto

Automation Foundry Ã© uma plataforma local para construir, executar e acompanhar automaÃ§Ãµes ou semi-automaÃ§Ãµes Ãºteis, demonstrÃ¡veis em portfÃ³lio e, quando validado, capazes de gerar ou economizar dinheiro.

Um Ãºnico control plane oferece:

- disparo manual e agendado;
- estado de runs e steps;
- aprovaÃ§Ãµes pendentes;
- retry, cancelamento e kill switch;
- artefatos e logs;
- mÃ©tricas de qualidade, custo, receita e valor entregue;
- saÃºde do computador, workers e connectors.

Cada domÃ­nio continua isolado como mÃ³dulo. Content Engine, Trading Lab e futuras automaÃ§Ãµes compartilham infraestrutura e contratos, nÃ£o suas regras de negÃ³cio.

## 2. Estado real da implementaÃ§Ã£o

Na data desta versÃ£o:

- `src/core/config.py`, `database.py` e modelos SQLAlchemy formam a base inicial;
- `src/pipeline/script_gen.py` implementa a geraÃ§Ã£o de roteiro do Content Engine e possui cobertura de testes relevante;
- o schema atual ainda Ã© orientado a conteÃºdo;
- Celery possui topologia local, probes de infraestrutura e dispatch durÃ¡vel para executores registrados; grande parte dos mÃ³dulos planejados permanece vazia ou incompleta, e o dashboard cobre visÃµes iniciais e controles locais limitados;
- metadata, defaults locais e nomes de serviÃ§os usam a identidade `automation-foundry`;
- `src/cli.py` oferece o diagnÃ³stico local `automation-foundry doctor` e inicia o dashboard com binding validado em loopback;
- `src/platform/` implementa parcialmente o kernel com `Automation`, `Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, `MetricPoint`, `Experiment`, `LedgerEntry`, `Alert` e histÃ³ricos persistidos de transiÃ§Ãµes;
- vinte e duas migrations Alembic incrementais cobrem as tabelas compartilhadas, o dispatch duravel, ocorrencias de schedule, estado de health, controles administrativos, continuacoes aprovadas, a evidencia de expurgo de artifacts, a escolha estruturada de approvals, observações de connectors e o baseline das oito tabelas legadas do Content Engine, mantendo `platform_alerts` separado de `alerts`;
- o caminho PostgreSQL local usa `asyncpg`, pool limitado, timeouts de conexÃ£o/comando e migrations validadas; SQLite permanece como bootstrap de processo Ãºnico e backend dos testes;
- `src/core/celery_app.py` declara somente as filas `gpu`, `cpu` e `io`, usa Redis como broker e backend efÃªmero de probes e recusa criaÃ§Ã£o implÃ­cita de filas; o entrypoint de worker exige PostgreSQL e serializa cada fila no Windows;
- `src/platform/dispatch_service.py` persiste a run e `delivery_id` antes da publicaÃ§Ã£o, registra tentativas e falhas de broker, concede claims com lease e impede que entregas duplicadas executem novamente uma run terminal; quando uma approval libera novo trabalho na mesma run, o dispatch concluido pode voltar auditadamente a `pending` com evento `requeued`, preservando a mesma identidade e deixando o executor decidir a fase atual pelo banco;
- `automation-foundry run-example` executa um Ãºnico passo `io` no-op, idempotente e com checkpoints transacionais, somente em processo local;
- `src/platform/executor_registry.py` permite disparo e retry somente para executores registrados em cÃ³digo; o smoke inline e o Content Engine A1 em background estao declarados, e retries elegiveis criam uma nova run ligada a falha retryable anterior com actor e motivo;
- `src/pipeline/a1_executor.py` adapta a geracao A1 existente ao contrato compartilhado: valida pauta, persona, formato e aberturas recentes, enfileira um step `gpu`, grava atomicamente um bundle JSON revisavel, registra artifact com retencao de 90 dias, metrica de tamanho e custo de API externa zero e move a run para `awaiting_approval`; a approval editorial fica ligada ao ID e SHA-256 do bundle; com TTS desabilitado a decisao encerra A1, e com TTS habilitado ela acorda a mesma run para A2 e para os gates/stages posteriores configurados;
- `src/pipeline/tts.py` implementa o contrato A2 independente do backend: exige a approval exata do bundle A1, executa na fila `gpu`, aceita somente WAV PCM S16LE mono validado, publica o arquivo por rename atomico e registra artifact, duracao, energia estimada, proveniencia/licenca declarada pelo adapter e custo externo zero; `disabled` permanece o default, e o unico backend real selecionavel e `kokoro_quality_test`, com pacote 0.9.4, revisao/peso/voz fixados, CUDA e eSpeak NG externo obrigatorios, chunking limitado e proveniencia marcada como nao aprovada para monetizacao; os demais testes usam adapter fake, e uma execução integral com `pm_alex` na RTX 3090 recebeu aprovação auditiva inicial sem resolver a proveniência comercial;
- `src/pipeline/narration_review.py` implementa o gate humano opcional entre A2 e A3. `CONTENT_NARRATION_REVIEW_ENABLED=true` exige TTS e continuação visual habilitados, reconfere bundle, step e WAV por tamanho, SHA-256 e metadados PCM, calcula a quantidade e as dimensões exatas exigidas por A3 e abre a approval `review_content_narration_a2`. O dashboard serve apenas o WAV verificado por uma rota local `no-store`/`nosniff`, sem renderizar seu caminho. Aprovar reabre o mesmo dispatch e os steps A1/A2 são replay idempotente antes de A3; rejeitar cancela, e qualquer adulteração impede a continuação. O gate não publica, envia nem realiza gastos;
- `src/pipeline/visuals.py` implementa o contrato A3 independente do backend: consome o bundle A1 aprovado e o WAV A2 verificados, calcula um visual a cada seis segundos com limite, exige proveniencia/licenca e direito comercial por asset, valida PNG completo nas dimensoes do formato, publica imagens e manifesto atomicamente e registra artifacts, contagem, cobertura, energia estimada e custo; A2 deixa a run aberta quando A3 esta habilitado, e somente A3 a conclui sem alterar o audio. `disabled` permanece o default; `local_assets_quality_test` importa na fila `io` somente PNGs selecionados pelo operador em uma raiz contida, com manifesto estrito, direitos explicitos e SHA-256 por item, sem download ou repeticao automatica. Isso habilita validacao local, mas nao substitui revisao humana nem prova juridica independente; FLUX.1 e provedores stock seguem bloqueados;
- `src/pipeline/captions.py` implementa o contrato A4 independente do backend: consome somente o WAV A2 verificado e a narracao ligada a approval A1, exige timestamps por palavra monotonicos e confinados a duracao, idioma PT, similaridade minima com o texto aprovado, proveniencia/licenca e direito comercial; publica ASS com karaoke por rename atomico e registra artifact, contagem, similaridade, energia estimada em GPU quando aplicavel e custo zero. `disabled` permanece o default; `approved_text_timing_quality_test` roda em `cpu`, distribui deterministicamente as palavras aprovadas pela duracao real do WAV e nao baixa ou transcreve nada. Ele habilita montagem local, mas nao detecta pausas ou deriva do TTS e exige revisao humana; Whisper e pesos seguem bloqueados;
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
- `src/pipeline/assembly.py` implementa o contrato A5 independente do backend: exige WAV, manifesto/imagens A3 e ASS A4 verificados por tamanho e SHA-256, reconcilia o manifesto com cada imagem e monta na fila `cpu`; o resultado precisa declarar H.264, AAC, legendas queimadas, licença e direito comercial, e o validador inspeciona boxes MP4, tracks, resolução e duração antes do rename atomico. Artifact, duração, tempo de render, bytes, energia CPU estimada e custo zero ficam observáveis. `disabled` permanece o default; `ffmpeg_quality_test` aceita somente o build externo Gyan 8.0.1/WinGet fixado por hashes, usa subprocess sem shell e confirma tracks/duração com ffprobe real. Uma amostra short sintética reproduzível passou e teve legenda visualmente confirmada, mas áudio/narração real, formato long, política de redistribuição e revisão editorial continuam pendentes;
- `src/intelligence/similarity.py` implementa o primeiro contrato A6 sem modelo externo: verifica o roteiro aprovado e o MP4 A5, compara unigramas por cosseno e trigramas por Jaccard contra uma janela configurável de runs anteriores da mesma automação que já passaram pelo gate, e registra relatório JSON, métricas, energia CPU estimada e custo externo zero. Score máximo igual ou superior ao limite abre alerta local e falha a run com bloqueio não retryable; nenhum upload existe. O escopo continua sendo a automação: não existe identidade de canal durável e validada no servidor para restringir a janela, porque `channels` é uma tabela legada sem nenhum caminho de criação, sem vínculo com `Run` e ligada a credenciais de publicação fora do escopo atual; criar esse contrato é decisão de produto e não foi fabricado aqui. A comparação lexical também não substitui uma futura análise semântica auditada;
- `src/pipeline/final_review.py` implementa o gate humano A7 sobre o vídeo final. Ele é desabilitado por padrão; habilitado, A6 deixa de concluir a run e A7 confere MP4 e relatório de originalidade por tamanho e SHA-256, exige que o relatório descreva exatamente aquele vídeo e não esteja bloqueado, e abre uma approval ligada ao digest dessas evidências, com projeção segura de resolução, duração, bytes, codecs, hashes e números de originalidade. Aprovar conclui a run local com `published=false` quando A8 está desabilitado, ou abre A8 na mesma run quando ambos os gates estão habilitados; rejeitar cancela pelo contrato compartilhado; qualquer alteração posterior do arquivo recusa a decisão sem transicionar a run. Nenhum upload, envio ou gasto existe, e o gate não autoriza publicação futura. O dashboard oferece a página `no-store` dedicada da approval e transmite o MP4 exato por uma rota própria, com `nosniff`, sem cache e com Range, sempre reconferindo hash e digest antes de servir; approval de outra ação recebe 404 e evidência adulterada recebe 409;
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
- `src/platform/task_runner.py` aplica timeout assÃ­ncrono, retries limitados e backoff exponencial; pedidos de cancelamento e kill switch sÃ£o consultados antes e depois de cada corrotina e entre tentativas;
- `control_events` mantem auditoria append-only de pedidos de cancelamento, habilitacao administrativa e mudancas do kill switch por automacao; cada mudanca administrativa ou operacional registra o actor local e o motivo informado pelo operador, e os controles por automação usam lock de linha no PostgreSQL para não duplicar evidência sob requisições concorrentes;
- approvals vinculam run, aÃ§Ã£o e digest do payload; uma decisÃ£o pode ainda persistir uma escolha estruturada pequena, não sensível e imutável, e uma decisão aprovada sÃ³ pode autorizar uma chave idempotente de task;
- artifacts vinculam arquivo local a run e, opcionalmente, step; o registro persiste somente caminho relativo e metadados apÃ³s validar confinamento em `STORAGE_ROOT`, tamanho e SHA-256;
- schedules nascem desabilitados, validam cron POSIX de cinco campos e timezone IANA, calculam `next_run_at` em UTC e auditam criaÃ§Ã£o, habilitaÃ§Ã£o, desabilitaÃ§Ã£o e cada ocorrÃªncia publicada ou ignorada; habilitação pela UI exige executor registrado e a mudança de estado bloqueia a linha no PostgreSQL para que requisições concorrentes produzam no máximo um evento;
- metric points sÃ£o observaÃ§Ãµes decimais append-only, idempotentes e atribuÃ­veis a automation, run e step; unidade, fonte, confianÃ§a opcional e timestamp permanecem explÃ­citos;
- experiments registram hipÃ³tese, variantes e mÃ©trica primÃ¡ria com lifecycle auditado; somente experiments em execuÃ§Ã£o podem receber novas runs atribuÃ­das, e hipÃ³teses e motivos permanecem redigidos no dashboard;
- ledger entries sÃ£o observaÃ§Ãµes financeiras decimais append-only, idempotentes e atribuÃ­veis a automation e, opcionalmente, run, step e metric point; tipo, categoria, moeda, fonte, confianÃ§a opcional e timestamp permanecem explÃ­citos, sem executar pagamentos ou criar promessas;
- platform alerts deduplicam ocorrÃªncias, escalam severidade enquanto abertos e auditam reconhecimento, resoluÃ§Ã£o e reabertura; nenhuma notificaÃ§Ã£o externa foi implementada;
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
- o runtime completo nÃ£o foi validado no computador de casa.

O roadmap deve evoluir essa base sem confundir placeholders com funcionalidades prontas e sem reescrever a parte testada apenas por estÃ©tica arquitetural.

## 3. Objetivos e nÃ£o objetivos

### Objetivos

- Operar localmente com configuraÃ§Ã£o reproduzÃ­vel.
- Adicionar novos mÃ³dulos por um contrato consistente.
- Tornar estado, falhas, pendÃªncias e resultados visÃ­veis.
- Medir custo e valor por automaÃ§Ã£o e experimento.
- Usar a RTX 3090 com fila serializada e modelos locais quando fizer sentido.
- Manter decisÃµes irreversÃ­veis ou externas sob controle humano.
- Produzir evidÃªncias de engenharia Ãºteis para currÃ­culo e portfÃ³lio.

### Fora do escopo inicial

- ServiÃ§o pÃºblico, SaaS ou acesso remoto pela internet.
- Multi-tenancy, Kubernetes e microserviÃ§os.
- Escala distribuÃ­da em vÃ¡rios computadores.
- Promessas de lucro ou execuÃ§Ã£o autÃ´noma irrestrita.
- Trading com dinheiro real.
- PublicaÃ§Ã£o, outreach ou gasto sem gate humano.

## 4. VisÃ£o do sistema

```text
â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚ Control plane local â€” FastAPI + Jinja/HTMX                  â”‚
â”‚ runs Â· steps Â· aprovaÃ§Ãµes Â· mÃ©tricas Â· custos Â· saÃºde       â”‚
â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                           â”‚ comandos e consultas
â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚ Plataforma compartilhada                                    â”‚
â”‚ contratos Â· scheduler Â· task wrappers Â· adapters Â· alerts   â”‚
â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
               â”‚                      â”‚
       â”Œâ”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”     â”Œâ”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
       â”‚ Redis + Celery â”‚     â”‚ PostgreSQL                  â”‚
       â”‚ gpu Â· cpu Â· io â”‚     â”‚ fonte durÃ¡vel da verdade   â”‚
       â””â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”˜     â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
               â”‚
â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚ MÃ³dulos de domÃ­nio                                           â”‚
â”‚ Content Engine Â· Trading Lab Â· futuras automaÃ§Ãµes           â”‚
â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
               â”‚
â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚ Adapters locais e externos                                   â”‚
â”‚ Ollama Â· filesystem Â· APIs oficiais Â· feeds Â· paper broker  â”‚
â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
```

Codex, Cursor, Claude, plugins e MCPs sÃ£o ferramentas de desenvolvimento. Nenhum deles faz parte do caminho obrigatÃ³rio de execuÃ§Ã£o do produto.

## 5. Estilo arquitetural

Use um monÃ³lito modular. Ele oferece fronteiras claras sem o custo operacional de microserviÃ§os em um sistema de uma pessoa e um computador.

Estrutura-alvo, migrada gradualmente:

```text
src/
â”œâ”€â”€ platform/
â”‚   â”œâ”€â”€ config/
â”‚   â”œâ”€â”€ database/
â”‚   â”œâ”€â”€ orchestration/
â”‚   â”œâ”€â”€ observability/
â”‚   â”œâ”€â”€ approvals/
â”‚   â””â”€â”€ adapters/
â”œâ”€â”€ modules/
â”‚   â”œâ”€â”€ content_engine/
â”‚   â”œâ”€â”€ trading_lab/
â”‚   â””â”€â”€ <future_module>/
â””â”€â”€ dashboard/
    â”œâ”€â”€ routers/
    â”œâ”€â”€ services/
    â””â”€â”€ templates/
tests/
â”œâ”€â”€ platform/
â””â”€â”€ modules/
```

A estrutura atual em `src/core`, `src/pipeline` e pastas relacionadas pode permanecer enquanto cada migraÃ§Ã£o tiver testes e benefÃ­cio concreto. Evite uma movimentaÃ§Ã£o em massa antes de obter um baseline verde.

## 6. Contrato compartilhado

Todo mÃ³dulo declara:

- `slug`, nome e estado habilitado/pausado;
- schema de entrada e de saÃ­da;
- trigger manual, agendado, evento ou API;
- steps ordenados e fila de cada step;
- chave de idempotÃªncia;
- retry, timeout, cancelamento e recuperaÃ§Ã£o;
- gates de aprovaÃ§Ã£o e kill switch;
- artefatos e polÃ­tica de retenÃ§Ã£o;
- secrets, connectors e rate limits necessÃ¡rios;
- health checks e comportamento degradado;
- mÃ©tricas operacionais, financeiras, de qualidade e de negÃ³cio.

O contrato nÃ£o obriga mÃ³dulos a compartilhar tabelas de domÃ­nio.

## 7. Modelo de estado

Conceitos genÃ©ricos da plataforma:

- `Automation`: registro e configuraÃ§Ã£o do mÃ³dulo.
- `Run`: uma execuÃ§Ã£o iniciada por um trigger.
- `StepRun`: execuÃ§Ã£o observÃ¡vel de uma etapa.
- `Approval`: decisÃ£o humana requerida.
- `Artifact`: arquivo ou resultado produzido.
- `Schedule`: agenda persistida.
- `MetricPoint`: observaÃ§Ã£o temporal.
- `ConnectorObservation`: estado redigido, freshness e qualidade de uma integração.
- `Experiment`: hipÃ³tese e variante sob avaliaÃ§Ã£o.
- `LedgerEntry`: custo, receita ou valor atribuÃ­vel.
- `Alert`: condiÃ§Ã£o operacional ou de risco.

Estados mÃ­nimos de uma run ou step:

```text
queued â†’ running â†’ succeeded
                 â†˜ failed â†’ queued (retry limitado)
queued/running â†’ cancelled
running â†’ awaiting_approval â†’ running (approved) / cancelled (rejected ou cancelamento)
```

Toda transiÃ§Ã£o Ã© persistida no banco com timestamps, tentativa, erro estruturado e relaÃ§Ã£o com a execuÃ§Ã£o anterior quando houver retry. DecisÃµes de approval sÃ£o imutÃ¡veis, auditadas e vinculadas ao digest do payload protegido. O `actor` informado pela CLI Ã© evidÃªncia operacional local, nÃ£o autenticaÃ§Ã£o forte.

## 8. ExecuÃ§Ã£o e agendamento

Celery executa as tarefas e Redis atua como broker e cache. Redis nÃ£o substitui a persistÃªncia.

- `gpu`: Ollama pesado, TTS, imagem, embeddings ou transcriÃ§Ã£o; concorrÃªncia 1.
- `cpu`: FFmpeg, transformaÃ§Ã£o de arquivos e cÃ¡lculo paralelo.
- `io`: APIs, scraping permitido, downloads, uploads e coleta de dados.

Tasks devem ser idempotentes, reconhecer cancelamento, usar limites de tempo e produzir erros acionÃ¡veis. O dispatch prepara estado no PostgreSQL antes de publicar, usa identidade estÃ¡vel, histÃ³rico append-only e claim com lease; uma mensagem concorrente aguarda a lease e uma mensagem terminal nÃ£o repete trabalho. O wrapper consulta controles persistidos antes e depois de cada corrotina e entre retries, mas nÃ£o preempta cÃ³digo sÃ­ncrono bloqueante. Um tick periÃ³dico do Beat consulta schedules no banco; cada horÃ¡rio previsto vira uma ocorrÃªncia Ãºnica, com misfire, sobreposiÃ§Ã£o e falha de publicaÃ§Ã£o observÃ¡veis. Beat roda por entrypoint separado, com PID file do Celery e named mutex adicional no Windows. n8n sÃ³ entra se integraÃ§Ãµes entre aplicaÃ§Ãµes justificarem outra camada operacional.

No Windows, `automation-foundry-runtime start` e o caminho operacional canonico. O processo permanece visivel em foreground, executa preflight e migrations antes de abrir processos host ocultos, registra estado local com identidade PID protegida contra reutilizacao e encerra o conjunto ao receber `stop`, Ctrl+C ou uma falha de filho. PostgreSQL e Redis que ja estavam ativos nao sao reivindicados nem parados pelo supervisor.

## 9. Control plane

A arquitetura do dashboard usa FastAPI, Jinja e HTMX e escuta apenas em `127.0.0.1` por padrao. A home, o detalhe de run, saúde e os fragmentos operacionais respondem `no-store`. O detalhe de run carrega HTMX 2.0.10 vendorizado no pacote, sem CDN, e consulta um fragmento a cada dois segundos somente enquanto a run nao e terminal. O fragmento reutiliza a mesma projecao redigida da pagina completa e substitui apenas status e steps. A home usa o mesmo asset local para atualizar a cada trinta segundos o fragmento de connectors, freshness e qualidade; o servidor seleciona a observação persistida mais recente, deixa estados vencerem para `stale`, prioriza indisponíveis/degradados, sinaliza listas limitadas e mostra o horário UTC absoluto sem tocar na integração. A fila liga approvals editoriais com evidência integral às suas páginas locais `no-store`; nelas, aprovação e rejeição usam formulários HTML nativos com POST seguido de redirect, confirmação e CSRF, e uma decisão já registrada torna a página somente leitura. Uma run Content Engine concluída por A7/A8 oferece uma página separada de export local, manifesto e downloads que reconferem toda a evidência antes de servir. O controle de schedule usa o mesmo padrão nativo e auditado: apenas habilita ou desabilita futuros disparos locais, recusa habilitação quando a automação está pausada, o kill switch está ativo ou não há executor registrado, e não renderiza o payload. Um bloqueio esperado retorna por redirect com aviso seguro; estado efetivo e causas allowlisted permanecem diagnosticáveis sem JavaScript. Os demais formulários e controles seguem o mesmo padrão.

VisÃµes mÃ­nimas:

- resumo de automaÃ§Ãµes, resultado e alertas;
- runs atuais, prÃ³ximas e recentes;
- steps, tentativas, duraÃ§Ã£o e erro;
- fila de aprovaÃ§Ãµes e idade da pendÃªncia;
- schedules e estado dos connectors;
- custo, receita e resultado lÃ­quido quando atribuÃ­vel;
- CPU, RAM, GPU, VRAM, disco, Redis, banco e workers;
- qualidade e freshness dos dados.

AÃ§Ãµes mÃ­nimas:

- iniciar uma run manual;
- cancelar ou pausar com seguranÃ§a;
- retentar uma etapa elegÃ­vel;
- aprovar ou rejeitar uma aÃ§Ã£o;
- habilitar ou desabilitar schedule e mÃ³dulo;
- acionar kill switch.

## 10. Dados e artefatos

PostgreSQL Ã© o alvo para execuÃ§Ã£o concorrente. SQLite continua Ãºtil para testes e protÃ³tipos de processo Ãºnico.

Arquivos grandes usam filesystem local com caminhos registrados no banco. A organizaÃ§Ã£o recomendada Ã©:

```text
storage/<automation_slug>/<run_id>/<artifact_type>/...
```

Cada artefato registra checksum, tamanho, tipo, origem, data, retenÃ§Ã£o e sensibilidade. A retenção vem de uma política declarativa por tipo de artifact, com produtor e justificativa explícitos; tipo não declarado fica com retenção indefinida, e o prazo é congelado na linha no momento do registro. O registro atual inspeciona arquivos jÃ¡ existentes, rejeita caminhos que resolvam fora de `STORAGE_ROOT` e nÃ£o cria, copia, altera nem apaga o conteÃºdo. O inventário de retenção reconcilia disco e registros e classifica cada item sem apagar nada. A exclusão material existe apenas no caminho de expurgo aprovado: desabilitada por default, ligada por SHA-256 à lista exata que um humano aprovou, com runtime parado, backup verificado prévio e reconferência de hash imediatamente antes de cada remoção. O registro do artifact sobrevive à remoção como evidência auditável. O backup trata banco, configuraÃ§Ã£o sem secrets e artefatos como um bundle versionado: publica somente depois de verificar hashes e formatos, exige o runtime parado e nunca arquiva logs, estado operacional ou backups anteriores. O restore exige `RESTORE <backup-id>`, compatibilidade do banco, storage de artefatos vazio e cria um bundle automÃ¡tico do estado anterior; PostgreSQL usa `pg_restore --single-transaction`, e artefatos novos sÃ£o removidos se o banco falhar. SHA-256 protege contra corrupÃ§Ã£o acidental, nÃ£o substitui assinatura contra adulteraÃ§Ã£o maliciosa.

## 11. Adapters e connectors

IntegraÃ§Ãµes de produto ficam atrÃ¡s de adapters prÃ³prios:

- modelo local por API do Ollama;
- APIs oficiais de plataformas e provedores;
- feeds de mercado e paper broker;
- notificaÃ§Ãµes e exports;
- filesystem e ferramentas de mÃ­dia.

Plugins e MCPs podem ajudar agentes a desenvolver ou investigar, mas nÃ£o substituem esses adapters. Todo adapter externo implementa timeout, retry limitado, rate limit, freshness, redaction de logs e modo fake para testes.

## 12. SeguranÃ§a e aprovaÃ§Ãµes

Exija aprovaÃ§Ã£o humana antes de:

- publicar conteÃºdo ou responder comentÃ¡rios;
- enviar outreach ou mensagens externas;
- gastar dinheiro ou consumir um serviÃ§o pago fora do limite aprovado;
- apagar dados ou artefatos materiais;
- executar qualquer aÃ§Ã£o financeira.

Cada mÃ³dulo com efeitos externos possui kill switch. Secrets permanecem fora do Git e recebem o menor escopo possÃ­vel. O dashboard local nÃ£o deve ser exposto publicamente por conveniÃªncia.

Trading Lab nunca permite que o LLM envie ordens. O caminho inicial termina em paper broker; qualquer proposta futura de dinheiro real exige decisÃ£o de arquitetura, threat model, limites, reconciliaÃ§Ã£o e autorizaÃ§Ã£o explÃ­cita separada.

## 13. IA e hardware local

O hardware esperado Ã© Ryzen 9 5950X, RTX 3090 com 24 GB de VRAM e aproximadamente 32 GB de RAM, ainda a confirmar no computador de casa.

- mantenha somente um workload pesado por vez na GPU;
- carregue e descarregue modelos de forma previsÃ­vel;
- registre modelo, versÃ£o, parÃ¢metros, duraÃ§Ã£o e uso estimado;
- ofereÃ§a caminhos mockados ou CPU-light para testes;
- nÃ£o afirme compatibilidade ou desempenho sem executar no hardware real.

## 14. Observabilidade e economia

Uma automaÃ§Ã£o deve provar mais que execuÃ§Ã£o tÃ©cnica. Registre:

- taxa de sucesso, duraÃ§Ã£o e retries;
- idade de pendÃªncias e tempo humano economizado;
- qualidade especÃ­fica do domÃ­nio;
- consumo de GPU/CPU e custos externos;
- receita ou valor atribuÃ­do com fonte e grau de confianÃ§a;
- resultado lÃ­quido por mÃ³dulo e experimento quando mensurÃ¡vel.

MÃ©tricas financeiras sÃ£o observaÃ§Ãµes, nÃ£o promessas. Trading usa mÃ©tricas de risco e validaÃ§Ã£o fora da amostra, nunca apenas retorno bruto.

## 15. MÃ³dulos iniciais

### Content Engine

Primeiro mÃ³dulo existente. Evolui de geraÃ§Ã£o de roteiro para pipeline editorial com TTS, visuais, legendas, montagem, similaridade, aprovaÃ§Ã£o, upload privado e analytics. Preserve licenÃ§as comerciais, variaÃ§Ã£o real e revisÃ£o humana.

### Trading Research Lab

Ambiente isolado para ingestÃ£o de dados, pesquisa reproduzÃ­vel, backtests sem vieses Ã³bvios, walk-forward e paper trading. Dinheiro real permanece fora do escopo.

### PrÃ³ximos mÃ³dulos candidatos

- monitor de oportunidades e preÃ§os;
- inteligÃªncia de mercado e concorrentes;
- pesquisa e qualificaÃ§Ã£o de leads com rascunhos aprovÃ¡veis;
- geraÃ§Ã£o de relatÃ³rios e briefs;
- automaÃ§Ãµes pessoais de alto valor mensurÃ¡vel.

Cada candidato passa pelo contrato de `$build-automation-module` e por avaliaÃ§Ã£o de valor, risco, custo de manutenÃ§Ã£o e qualidade dos dados.

## 16. DecisÃµes arquiteturais vigentes

1. Local-first, sem deployment pÃºblico inicial.
2. MonÃ³lito modular antes de microserviÃ§os.
3. FastAPI + HTMX antes de SPA.
4. PostgreSQL para concorrÃªncia; SQLite para testes e bootstrap.
5. Celery + Redis + Beat antes de n8n.
6. Banco como fonte de verdade e filesystem para artefatos.
7. GPU serializada.
8. Adapters da aplicaÃ§Ã£o, nÃ£o MCPs, no runtime.
9. Gates humanos para efeitos externos ou irreversÃ­veis.
10. Backtest e paper trading antes de qualquer discussÃ£o de live trading.

## 17. DefiniÃ§Ã£o arquitetural de pronto

Uma fatia estÃ¡ pronta quando:

- o contrato e os estados sÃ£o explÃ­citos;
- o resultado Ã© observÃ¡vel no control plane;
- retry, duplicaÃ§Ã£o, falha parcial e cancelamento tÃªm comportamento seguro;
- gates e kill switch funcionam quando aplicÃ¡veis;
- testes relevantes passam;
- secrets e dados sensÃ­veis estÃ£o protegidos;
- mÃ©tricas e custos essenciais sÃ£o registrados;
- documentaÃ§Ã£o corresponde Ã  implementaÃ§Ã£o;
- verificaÃ§Ãµes dependentes do computador de casa estÃ£o executadas ou claramente pendentes.

## 18. ReferÃªncia histÃ³rica

A especificaÃ§Ã£o detalhada original do Content Engine foi preservada em `docs/reference/content-engine-v1.md`. Ela pode orientar o mÃ³dulo, mas nÃ£o substitui esta arquitetura nem o roadmap atual.
