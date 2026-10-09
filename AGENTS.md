# Automation Foundry — regras para agentes de código

## Ordem obrigatória de leitura

1. Leia `AGENTS.md`, `ARQUITETURA.md` e `ROADMAP.md` antes de alterar código.
2. Carregue a skill adequada em `.agents/skills/`.
3. Inspecione a implementação e os testes afetados. Documentação e arquivos vazios não provam que uma funcionalidade existe.
4. Trabalhe em uma fatia vertical pequena e verificável por vez.

## Contexto do produto

Automation Foundry é uma plataforma local para executar automações independentes por meio de um único control plane. O runtime deve funcionar no computador principal do proprietário sem depender de Codex, Cursor, Claude, plugins ou MCPs.

Estado atual: a base de configuração, banco e modelos existe; a geração de roteiro do Content Engine possui implementação e testes; grande parte do restante ainda é especificação ou placeholder. Não descreva itens do roadmap como implementados.

## Roteamento por skill

- Mudanças de arquitetura, roadmap ou contratos compartilhados: `$operate-automation-foundry`.
- Dashboard, persistência, filas, agendamento e observabilidade: `$build-control-plane`.
- Pipeline editorial e audiovisual: `$build-content-engine`.
- Pesquisa quantitativa, backtest e paper trading: `$build-trading-lab`.
- Novo domínio de automação: `$build-automation-module`.
- Auditoria de prontidão ou decisão do próximo passo: `$run-project-checkpoint`.

`.agents/skills/` é a fonte canônica. `.claude/skills/` contém apenas wrappers para Claude Code.

## Decisões fixas

- Runtime local-first e dashboard ligado apenas a loopback por padrão.
- Monorepo modular em Python 3.12.
- FastAPI + Jinja/HTMX no control plane inicial.
- SQLAlchemy 2.0 + Alembic; PostgreSQL para workers concorrentes e SQLite apenas para testes ou desenvolvimento inicial de processo único.
- Celery + Redis para execução; Celery Beat para o agendamento inicial. n8n permanece adiado até existir necessidade comprovada.
- Filas separadas: `gpu`, `cpu` e `io`. A fila `gpu` usa concorrência 1.
- Ollama e demais modelos são acessados diretamente por adapters da aplicação.
- Banco é a fonte da verdade; Redis não é armazenamento durável de estado.
- Artefatos grandes ficam no filesystem local e não no Git.

Não introduza hospedagem pública, SaaS, Kubernetes, microserviços, multi-tenancy, autenticação remota ou uma SPA separada sem mudança explícita de escopo.

## Segurança e limites de produto

- Exija aprovação humana antes de publicar, enviar mensagens, gastar dinheiro, apagar dados ou executar ações financeiras.
- Mantenha qualquer negociação com dinheiro real desabilitada. Trading Lab começa com pesquisa, backtest e paper trading.
- Nunca permita que um LLM envie ordens diretamente.
- Implemente kill switch e limites explícitos para módulos com efeitos externos.
- Use somente APIs oficiais e respeite licenças, termos de plataforma, rate limits e privacidade.
- Nunca versione `.env`, tokens, credenciais, dados pessoais ou artefatos privados.
- Não registre segredos em logs. Criptografe credenciais persistidas quando necessário.
- Content Engine: preserve o gate editorial, o bloqueio de similaridade e as restrições de licenciamento definidas em sua skill.

## Convenções de implementação

- Type hints em toda função pública e modelos explícitos nas fronteiras.
- Logs estruturados e erros com contexto suficiente para diagnóstico.
- Tasks idempotentes, com chave estável, retries limitados, backoff exponencial, timeouts e cancelamento seguro.
- Registre toda transição de run e step no banco.
- Isole integrações externas atrás de adapters substituíveis.
- Adicione testes junto com o código. Mocke serviços pagos, GPUs, APIs e ações irreversíveis por padrão.
- Use migrations para mudanças persistentes de schema.
- Preserve alterações do usuário e evite reescritas amplas sem necessidade.

## Fluxo de mudança

1. Declare resultado esperado, módulos afetados e riscos.
2. Defina entradas, saídas, estados, falhas, aprovações, métricas e custos.
3. Implemente a menor fatia que gere resultado observável.
4. Execute verificações proporcionais ao risco.
5. Atualize arquitetura e roadmap quando contratos ou prioridades mudarem.
6. Informe claramente o que foi verificado e o que depende do computador de casa, GPU, credenciais ou revisão manual.

## Git e atribuição

- Commits pequenos e intencionais; não misture mudanças não relacionadas.
- Não inclua `Co-authored-by`, `Made-with`, `Generated-with` ou qualquer atribuição de IA em commits e pull requests, salvo pedido explícito do usuário.
- Não faça merge em `main` nem abra PR sem autorização explícita.

## Definição mínima de pronto

Uma mudança só está pronta quando contratos e estados estão explícitos, falhas são seguras e observáveis, testes relevantes passam, segredos não foram versionados e validações dependentes de hardware foram registradas como pendentes em vez de presumidas.
