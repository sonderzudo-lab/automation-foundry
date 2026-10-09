# Automation Foundry

Plataforma local-first para rodar automações com controle humano: um único control plane dispara,
agenda, acompanha e audita execuções que rodam em workers no meu próprio computador.

O primeiro módulo é o **Content Engine**, um pipeline de vídeo que gera roteiro, narração, visuais,
legendas e a montagem final sem chamar nenhuma API paga.

> **Status:** em desenvolvimento. O kernel da plataforma, a fila e o dashboard têm código real e
> testado; parte das etapas do Content Engine funciona em modo de teste local e outros módulos ainda
> são planejados. O estado detalhado e honesto de cada área está em
> [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

## Arquitetura

```
              ┌──────────────────────────────┐
              │  Dashboard (FastAPI + HTMX)  │  aprovações, cancelamento,
              │  CLI  automation-foundry     │  kill switch, saúde
              └──────────────┬───────────────┘
                             │ cria Run + dispatch (persistido antes de publicar)
                             ▼
┌──────────────┐     ┌───────────────┐     ┌──────────────────────────────┐
│ PostgreSQL   │◄────┤ Redis (broker)├────►│ Celery workers               │
│ fonte da     │     └───────────────┘     │  gpu (concorrência 1)        │
│ verdade      │◄──────────────────────────┤  cpu                         │
└──────────────┘   transições de run/step  │  io                          │
                                           └──────────────┬───────────────┘
                                                          ▼
                                     Ollama · Kokoro · FFmpeg · storage local
```

**Decisões principais**

- **Banco é a fonte da verdade, Redis é só transporte.** A run e o ID de entrega são gravados no
  PostgreSQL antes de publicar na fila. Se o broker falha, a entrega continua reenviável.
- **Entrega idempotente.** Workers pegam a run com claim e lease no banco; uma mensagem duplicada
  nunca executa de novo um step que já terminou.
- **Três filas fixas** (`gpu`, `cpu`, `io`). A fila de GPU tem concorrência 1, então só um
  modelo pesado usa a placa por vez.
- **Tudo auditável.** Cada transição de run e step, aprovação, cancelamento e alerta vira um evento
  append-only.
- **Humano no loop.** Nada é publicado, enviado ou gasto sem aprovação. Um kill switch persistente
  para tudo.

## O que já funciona

| Área | Estado |
| --- | --- |
| Kernel (`Automation`, `Run`, `StepRun`, `Approval`, `Artifact`, `Schedule`, métricas, alertas) | Implementado |
| Dispatch durável com Celery + Redis, retries com backoff, timeout e cancelamento | Implementado |
| Schedules com Celery Beat, sem execução duplicada por horário | Implementado |
| Dashboard local com detalhe de run, aprovações e saúde dos serviços | Parcial |
| Backup e restore verificados por SHA-256 (SQLite e PostgreSQL) | Implementado |
| Content Engine: roteiro com LLM local + revisão editorial | Implementado |
| Content Engine: narração, visuais, legendas, montagem e gate de originalidade | Modo de teste local |
| Publicação automática, análise de receita, outros domínios | Planejado |

## Stack

Python 3.12 · FastAPI · Jinja + HTMX · SQLAlchemy 2.0 async · Alembic · PostgreSQL 17 · Redis ·
Celery · Docker Compose · Ollama · Kokoro · FFmpeg · pytest · ruff · mypy

## Rodando localmente

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

cp .env.example .env            # defina POSTGRES_PASSWORD e a DATABASE_URL
docker compose up -d postgres redis
.venv/bin/alembic upgrade head

.venv/bin/automation-foundry doctor                                 # diagnóstico
.venv/bin/automation-foundry run-example --idempotency-key smoke-1  # run de exemplo
```

O runtime completo (workers, Beat e dashboard) é gerenciado por `automation-foundry-runtime start`.
Os comandos para Windows e o passo a passo de backup estão em
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

## Estrutura

```
src/
  core/        config, banco, app Celery
  platform/    kernel: runs, dispatch, aprovações, schedules, ledger, alertas
  dashboard/   control plane (FastAPI + templates HTMX)
  pipeline/    etapas do Content Engine (roteiro, TTS, visuais, legendas, montagem)
  operations/  saúde, alertas de saúde, backup/restore
  runtime/     supervisor que sobe e derruba a topologia local
alembic/       migrations do schema
tests/         suíte com mais de 350 testes
```

Mais detalhes em [`ARQUITETURA.md`](ARQUITETURA.md) e [`ROADMAP.md`](ROADMAP.md).

## Autor

Kauê Prata · [kaueprata.com](https://kaueprata.com) · [LinkedIn](https://linkedin.com/in/kauefpg)
