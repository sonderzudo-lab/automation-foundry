# Automation Foundry â€” regras para agentes de cÃ³digo

## Ordem obrigatÃ³ria de leitura

1. Leia `AGENTS.md`, `ARQUITETURA.md` e `ROADMAP.md` antes de alterar cÃ³digo.
2. Carregue a skill adequada em `.agents/skills/`.
3. Inspecione a implementaÃ§Ã£o e os testes afetados. DocumentaÃ§Ã£o e arquivos vazios nÃ£o provam que uma funcionalidade existe.
4. Trabalhe em uma fatia vertical pequena e verificÃ¡vel por vez.

## Contexto do produto

Automation Foundry Ã© uma plataforma local para executar automaÃ§Ãµes independentes por meio de um Ãºnico control plane. O runtime deve funcionar no computador principal do proprietÃ¡rio sem depender de Codex, Cursor, Claude, plugins ou MCPs.

Estado atual: a base de configuraÃ§Ã£o, banco e modelos existe; a geraÃ§Ã£o de roteiro do Content Engine possui implementaÃ§Ã£o e testes; grande parte do restante ainda Ã© especificaÃ§Ã£o ou placeholder. NÃ£o descreva itens do roadmap como implementados.

## Roteamento por skill

- MudanÃ§as de arquitetura, roadmap ou contratos compartilhados: `$operate-automation-foundry`.
- Dashboard, persistÃªncia, filas, agendamento e observabilidade: `$build-control-plane`.
- Pipeline editorial e audiovisual: `$build-content-engine`.
- Pesquisa quantitativa, backtest e paper trading: `$build-trading-lab`.
- Novo domÃ­nio de automaÃ§Ã£o: `$build-automation-module`.
- Auditoria de prontidÃ£o ou decisÃ£o do prÃ³ximo passo: `$run-project-checkpoint`.

`.agents/skills/` Ã© a fonte canÃ´nica. `.claude/skills/` contÃ©m apenas wrappers para Claude Code.

## DecisÃµes fixas

- Runtime local-first e dashboard ligado apenas a loopback por padrÃ£o.
- Monorepo modular em Python 3.12.
- FastAPI + Jinja/HTMX no control plane inicial.
- SQLAlchemy 2.0 + Alembic; PostgreSQL para workers concorrentes e SQLite apenas para testes ou desenvolvimento inicial de processo Ãºnico.
- Celery + Redis para execuÃ§Ã£o; Celery Beat para o agendamento inicial. n8n permanece adiado atÃ© existir necessidade comprovada.
- Filas separadas: `gpu`, `cpu` e `io`. A fila `gpu` usa concorrÃªncia 1.
- Ollama e demais modelos sÃ£o acessados diretamente por adapters da aplicaÃ§Ã£o.
- Banco Ã© a fonte da verdade; Redis nÃ£o Ã© armazenamento durÃ¡vel de estado.
- Artefatos grandes ficam no filesystem local e nÃ£o no Git.

NÃ£o introduza hospedagem pÃºblica, SaaS, Kubernetes, microserviÃ§os, multi-tenancy, autenticaÃ§Ã£o remota ou uma SPA separada sem mudanÃ§a explÃ­cita de escopo.

## SeguranÃ§a e limites de produto

- Exija aprovaÃ§Ã£o humana antes de publicar, enviar mensagens, gastar dinheiro, apagar dados ou executar aÃ§Ãµes financeiras.
- Mantenha qualquer negociaÃ§Ã£o com dinheiro real desabilitada. Trading Lab comeÃ§a com pesquisa, backtest e paper trading.
- Nunca permita que um LLM envie ordens diretamente.
- Implemente kill switch e limites explÃ­citos para mÃ³dulos com efeitos externos.
- Use somente APIs oficiais e respeite licenÃ§as, termos de plataforma, rate limits e privacidade.
- Nunca versione `.env`, tokens, credenciais, dados pessoais ou artefatos privados.
- NÃ£o registre segredos em logs. Criptografe credenciais persistidas quando necessÃ¡rio.
- Content Engine: preserve o gate editorial, o bloqueio de similaridade e as restriÃ§Ãµes de licenciamento definidas em sua skill.

## ConvenÃ§Ãµes de implementaÃ§Ã£o

- Type hints em toda funÃ§Ã£o pÃºblica e modelos explÃ­citos nas fronteiras.
- Logs estruturados e erros com contexto suficiente para diagnÃ³stico.
- Tasks idempotentes, com chave estÃ¡vel, retries limitados, backoff exponencial, timeouts e cancelamento seguro.
- Registre toda transiÃ§Ã£o de run e step no banco.
- Isole integraÃ§Ãµes externas atrÃ¡s de adapters substituÃ­veis.
- Adicione testes junto com o cÃ³digo. Mocke serviÃ§os pagos, GPUs, APIs e aÃ§Ãµes irreversÃ­veis por padrÃ£o.
- Use migrations para mudanÃ§as persistentes de schema.
- Preserve alteraÃ§Ãµes do usuÃ¡rio e evite reescritas amplas sem necessidade.

## Fluxo de mudanÃ§a

1. Declare resultado esperado, mÃ³dulos afetados e riscos.
2. Defina entradas, saÃ­das, estados, falhas, aprovaÃ§Ãµes, mÃ©tricas e custos.
3. Implemente a menor fatia que gere resultado observÃ¡vel.
4. Execute verificaÃ§Ãµes proporcionais ao risco.
5. Atualize arquitetura e roadmap quando contratos ou prioridades mudarem.
6. Informe claramente o que foi verificado e o que depende do computador de casa, GPU, credenciais ou revisÃ£o manual.

## Git e atribuiÃ§Ã£o

- Commits pequenos e intencionais; nÃ£o misture mudanÃ§as nÃ£o relacionadas.
- NÃ£o inclua `Co-authored-by`, `Made-with`, `Generated-with` ou qualquer atribuiÃ§Ã£o de IA em commits e pull requests, salvo pedido explÃ­cito do usuÃ¡rio.
- NÃ£o faÃ§a merge em `main` nem abra PR sem autorizaÃ§Ã£o explÃ­cita.

## DefiniÃ§Ã£o mÃ­nima de pronto

Uma mudanÃ§a sÃ³ estÃ¡ pronta quando contratos e estados estÃ£o explÃ­citos, falhas sÃ£o seguras e observÃ¡veis, testes relevantes passam, segredos nÃ£o foram versionados e validaÃ§Ãµes dependentes de hardware foram registradas como pendentes em vez de presumidas.
