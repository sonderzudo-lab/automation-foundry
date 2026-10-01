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
- dezenove migrations Alembic incrementais cobrem as tabelas compartilhadas, o dispatch duravel, ocorrencias de schedule, estado de health, controles administrativos, continuacoes aprovadas e o baseline das oito tabelas legadas do Content Engine, mantendo `platform_alerts` separado de `alerts`;
- o caminho PostgreSQL local usa `asyncpg`, pool limitado, timeouts de conexão/comando e migrations validadas; SQLite permanece como bootstrap de processo único e backend dos testes;
- `src/core/celery_app.py` declara somente as filas `gpu`, `cpu` e `io`, usa Redis como broker e backend efêmero de probes e recusa criação implícita de filas; o entrypoint de worker exige PostgreSQL e serializa cada fila no Windows;
- `src/platform/dispatch_service.py` persiste a run e `delivery_id` antes da publicação, registra tentativas e falhas de broker, concede claims com lease e impede que entregas duplicadas executem novamente uma run terminal; quando uma approval libera novo trabalho na mesma run, o dispatch concluido pode voltar auditadamente a `pending` com evento `requeued`, preservando a mesma identidade e deixando o executor decidir a fase atual pelo banco;
- `automation-foundry run-example` executa um único passo `io` no-op, idempotente e com checkpoints transacionais, somente em processo local;
- `src/platform/executor_registry.py` permite disparo e retry somente para executores registrados em código; o smoke inline e o Content Engine A1 em background estao declarados, e retries elegiveis criam uma nova run ligada a falha retryable anterior com actor e motivo;
- `src/pipeline/a1_executor.py` adapta a geracao A1 existente ao contrato compartilhado: valida pauta, persona, formato e aberturas recentes, enfileira um step `gpu`, grava atomicamente um bundle JSON revisavel, registra artifact com retencao de 90 dias, metrica de tamanho e custo de API externa zero e move a run para `awaiting_approval`; a approval editorial fica ligada ao ID e SHA-256 do bundle; com TTS desabilitado a decisao encerra A1, e uma configuracao futura habilitada acordara a mesma run para o step A2;
- `src/pipeline/tts.py` implementa o contrato A2 independente do backend: exige a approval exata do bundle A1, executa na fila `gpu`, aceita somente WAV PCM S16LE mono validado, publica o arquivo por rename atomico e registra artifact, duracao, energia estimada, proveniencia/licenca declarada pelo adapter e custo externo zero; `disabled` permanece o default, e o unico backend real selecionavel e `kokoro_quality_test`, com pacote 0.9.4, revisao/peso/voz fixados, CUDA e eSpeak NG externo obrigatorios, chunking limitado e proveniencia marcada como nao aprovada para monetizacao; os demais testes usam adapter fake e a escuta PT-BR na RTX 3090 continua pendente;
- `src/pipeline/visuals.py` implementa o contrato A3 independente do backend: consome o bundle A1 aprovado e o WAV A2 verificados, calcula um visual a cada seis segundos com limite, exige proveniencia/licenca e direito comercial por asset, valida PNG completo nas dimensoes do formato, publica imagens e manifesto atomicamente e registra artifacts, contagem, cobertura, energia estimada e custo; A2 deixa a run aberta quando A3 esta habilitado, e somente A3 a conclui sem alterar o audio. `disabled` permanece o default; `local_assets_quality_test` importa na fila `io` somente PNGs selecionados pelo operador em uma raiz contida, com manifesto estrito, direitos explicitos e SHA-256 por item, sem download ou repeticao automatica. Isso habilita validacao local, mas nao substitui revisao humana nem prova juridica independente; FLUX.1 e provedores stock seguem bloqueados;
- `src/pipeline/captions.py` implementa o contrato A4 independente do backend: consome somente o WAV A2 verificado e a narracao ligada a approval A1, exige timestamps por palavra monotonicos e confinados a duracao, idioma PT, similaridade minima com o texto aprovado, proveniencia/licenca e direito comercial; publica ASS com karaoke por rename atomico e registra artifact, contagem, similaridade, energia estimada em GPU quando aplicavel e custo zero. `disabled` permanece o default; `approved_text_timing_quality_test` roda em `cpu`, distribui deterministicamente as palavras aprovadas pela duracao real do WAV e nao baixa ou transcreve nada. Ele habilita montagem local, mas nao detecta pausas ou deriva do TTS e exige revisao humana; Whisper e pesos seguem bloqueados;
- `src/pipeline/assembly.py` implementa o contrato A5 independente do backend: exige WAV, manifesto/imagens A3 e ASS A4 verificados por tamanho e SHA-256, reconcilia o manifesto com cada imagem e monta na fila `cpu`; o resultado precisa declarar H.264, AAC, legendas queimadas, licença e direito comercial, e o validador inspeciona boxes MP4, tracks, resolução e duração antes do rename atomico. Artifact, duração, tempo de render, bytes, energia CPU estimada e custo zero ficam observáveis. `disabled` permanece o default; `ffmpeg_quality_test` aceita somente o build externo Gyan 8.0.1/WinGet fixado por hashes, usa subprocess sem shell e confirma tracks/duração com ffprobe real. Uma amostra short sintética reproduzível passou e teve legenda visualmente confirmada, mas áudio/narração real, formato long, política de redistribuição e revisão editorial continuam pendentes;
- `src/intelligence/similarity.py` implementa o primeiro contrato A6 sem modelo externo: verifica o roteiro aprovado e o MP4 A5, compara unigramas por cosseno e trigramas por Jaccard contra uma janela configurável de runs anteriores da mesma automação que já passaram pelo gate, e registra relatório JSON, métricas, energia CPU estimada e custo externo zero. Score máximo igual ou superior ao limite abre alerta local e falha a run com bloqueio não retryable; nenhum upload existe. O escopo ainda não distingue canais dentro da automação e a comparação lexical não substitui uma futura análise semântica auditada;
- `src/platform/task_runner.py` aplica timeout assíncrono, retries limitados e backoff exponencial; pedidos de cancelamento e kill switch são consultados antes e depois de cada corrotina e entre tentativas;
- `control_events` mantem auditoria append-only de pedidos de cancelamento, habilitacao administrativa e mudancas do kill switch por automacao; cada mudanca administrativa ou operacional registra o actor local e o motivo informado pelo operador;
- approvals vinculam run, ação e digest do payload; uma decisão aprovada só pode autorizar uma chave idempotente de task;
- artifacts vinculam arquivo local a run e, opcionalmente, step; o registro persiste somente caminho relativo e metadados após validar confinamento em `STORAGE_ROOT`, tamanho e SHA-256;
- schedules nascem desabilitados, validam cron POSIX de cinco campos e timezone IANA, calculam `next_run_at` em UTC e auditam criação, habilitação, desabilitação e cada ocorrência publicada ou ignorada;
- metric points são observações decimais append-only, idempotentes e atribuíveis a automation, run e step; unidade, fonte, confiança opcional e timestamp permanecem explícitos;
- experiments registram hipótese, variantes e métrica primária com lifecycle auditado; somente experiments em execução podem receber novas runs atribuídas, e hipóteses e motivos permanecem redigidos no dashboard;
- ledger entries são observações financeiras decimais append-only, idempotentes e atribuíveis a automation e, opcionalmente, run, step e metric point; tipo, categoria, moeda, fonte, confiança opcional e timestamp permanecem explícitos, sem executar pagamentos ou criar promessas;
- platform alerts deduplicam ocorrências, escalam severidade enquanto abertos e auditam reconhecimento, resolução e reabertura; nenhuma notificação externa foi implementada;
- `src/dashboard/` renderiza com FastAPI e Jinja uma visao geral, schedules redigidos e detalhe de run com evidencias vinculadas; schedules expoem estado, cron, timezone, proxima execucao e ultima ocorrencia, approvals pendentes exibem idade calculada no servidor, o ledger possui totais globais e por automacao separados por moeda e cada automacao mostra taxa de sucesso e duracao media sobre ate 50 resultados succeeded/failed recentes, nunca payload, categoria ou fonte internos; o detalhe de run usa HTMX vendorizado localmente para atualizar apenas status e steps enquanto a run nao e terminal, reutilizando a mesma projecao redigida; habilitacao administrativa, disparo registrado, retry elegivel, kill switch, cancelamento, rejeicao e aprovacao usam POST com confirmacao e token CSRF por processo; desabilitar uma automacao bloqueia novas runs e o inicio das enfileiradas sem interromper runs ativas, enquanto o kill switch preserva a semantica operacional; aprovacao positiva exige uma projecao limitada, validada e explicitamente segura do payload, e registros legados sem essa projecao permanecem bloqueados; a approval A1 oferece ainda uma pagina `no-store` com angulo, estrutura, hook, roteiro e narracao completos lidos do bundle verificado, omitindo persona e contexto privado do prompt; quando o dominio declara continuacao habilitada, a mesma transacao reabre o dispatch e a publicacao posterior permanece recuperavel se o broker falhar;
- `src/operations/health.py` coleta um snapshot somente leitura e redigido de CPU, memoria, disco, GPU/VRAM, banco, Redis, Beat e workers por fila; Redis indisponivel impede apenas a inspecao efemera dos workers e nunca substitui o estado duravel no banco;
- `src/operations/health_alerts.py` mantem contadores consecutivos no banco; duas degradacoes abrem ou atualizam um alerta deduplicado e duas recuperacoes o resolvem, enquanto `skip` permanece inconclusivo; a task periodica roda na fila `io` e nao envia notificacoes externas;
- `src/operations/backup.py` cria e restaura bundles atomicos e verificaveis com snapshot SQLite ou PostgreSQL, configuracao allowlisted sem secrets, artefatos e manifesto SHA-256; restore exige confirmacao exata, runtime parado, storage de artefatos vazio e um backup automatico do estado anterior;
- `src/runtime/` e `src/runtime_cli.py` implementam um supervisor Windows singleton em foreground para PostgreSQL, Redis, os tres workers, Beat e dashboard; processos host usam eventos nomeados para shutdown cooperativo e um Job Object kill-on-close como fallback, enquanto o Compose para somente servicos iniciados pelo proprio supervisor e nunca remove volumes;
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

A arquitetura do dashboard usa FastAPI, Jinja e HTMX e escuta apenas em `127.0.0.1` por padrao. O detalhe de run carrega HTMX 2.0.10 vendorizado no pacote, sem CDN, e consulta um fragmento `no-store` a cada dois segundos somente enquanto a run nao e terminal. O fragmento reutiliza a mesma projecao redigida da pagina completa e substitui apenas status e steps. Formularios e controles continuam HTML nativo com POST seguido de redirect, de modo que a operacao permanece funcional sem JavaScript; as demais visoes ainda nao possuem interacoes HTMX.

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

Cada artefato registra checksum, tamanho, tipo, origem, data, retenção e sensibilidade. O registro atual inspeciona arquivos já existentes, rejeita caminhos que resolvam fora de `STORAGE_ROOT` e não cria, copia, altera nem apaga o conteúdo. Retenção é metadado nesta fase; qualquer exclusão material continua exigindo aprovação humana e ainda não foi implementada. O backup trata banco, configuração sem secrets e artefatos como um bundle versionado: publica somente depois de verificar hashes e formatos, exige o runtime parado e nunca arquiva logs, estado operacional ou backups anteriores. O restore exige `RESTORE <backup-id>`, compatibilidade do banco, storage de artefatos vazio e cria um bundle automático do estado anterior; PostgreSQL usa `pg_restore --single-transaction`, e artefatos novos são removidos se o banco falhar. SHA-256 protege contra corrupção acidental, não substitui assinatura contra adulteração maliciosa.

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
