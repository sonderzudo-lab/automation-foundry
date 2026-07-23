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
- Celery, dashboard e grande parte dos mÃ³dulos planejados permanecem vazios ou incompletos;
- metadata, defaults locais e nomes de serviÃ§os usam a identidade `automation-foundry`;
- `src/cli.py` oferece o diagnÃ³stico local `automation-foundry doctor` sem alterar estado;
- `src/platform/` implementa parcialmente o kernel com `Automation`, `Run`, `StepRun`, `Approval` e histÃ³ricos persistidos de transiÃ§Ãµes;
- quatro migrations Alembic incrementais cobrem somente essas tabelas compartilhadas; o schema legado do Content Engine ainda nÃ£o possui baseline;
- `automation-foundry run-example` executa um Ãºnico passo `io` no-op, idempotente e com checkpoints transacionais, somente em processo local;
- `src/platform/task_runner.py` aplica timeout assÃ­ncrono, retries limitados e backoff exponencial; pedidos de cancelamento e kill switch sÃ£o consultados antes e depois de cada corrotina e entre tentativas;
- `control_events` mantÃ©m auditoria append-only de pedidos de cancelamento e mudanÃ§as do kill switch por automaÃ§Ã£o;
- approvals vinculam run, aÃ§Ã£o e digest do payload; uma decisÃ£o aprovada sÃ³ pode autorizar uma chave idempotente de task;
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

Tasks devem ser idempotentes, reconhecer cancelamento, usar limites de tempo e produzir erros acionÃ¡veis. O wrapper inicial consulta controles persistidos antes e depois de cada corrotina e entre retries, mas nÃ£o preempta cÃ³digo sÃ­ncrono bloqueante nem substitui os soft/hard limits futuros do Celery. Celery Beat Ã© o scheduler inicial e deve rodar como singleton. n8n sÃ³ entra se integraÃ§Ãµes entre aplicaÃ§Ãµes justificarem outra camada operacional.

## 9. Control plane

O dashboard inicial usa FastAPI, Jinja e HTMX e escuta apenas em `127.0.0.1` por padrÃ£o.

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

Cada artefato registra checksum, tamanho, tipo, origem, data, retenÃ§Ã£o e sensibilidade. Backups devem tratar banco, configuraÃ§Ã£o sem secrets e artefatos como conjuntos relacionados.

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
