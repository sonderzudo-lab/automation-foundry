# Estado real da implementação

**Checkpoint:** 2026-07-22

**Escopo:** Fase 1 — baseline e identidade técnica

**Branch:** `chore/project-skills`

## Veredito

O repositório possui uma base parcial e testável do Content Engine, mas ainda não possui o
runtime da plataforma. A geração de roteiro A1, a configuração, a sessão de banco e os modelos
SQLAlchemy orientados a conteúdo têm código real. Celery, control plane e as demais etapas de
automação são placeholders ou itens planejados.

## Classificação por área

| Área | Estado | Evidência e limite atual |
| --- | --- | --- |
| Identidade técnica | Implementado | `pyproject.toml`, `.env.example`, `src/core/config.py` e `docker-compose.yml` usam `automation-foundry` nos identificadores ativos. “Content Engine” permanece como nome legítimo do módulo. |
| Configuração | Parcial | `src/core/config.py` implementa settings tipados, leitura de `.env` e defaults locais. Campos de credenciais existem, mas criptografia, validação operacional e adapters consumidores ainda não existem. |
| Banco e sessão | Parcial | `src/core/database.py` cria engine e sessão async com commit/rollback. O default de processo único é SQLite; o caminho PostgreSQL concorrente ainda não foi implementado nem validado. |
| Modelos de domínio | Parcial | `src/core/models.py` implementa `Channel`, `Video`, `Job`, `Metric`, `Cost`, `Topic`, `Alert` e `ABVariant`. O schema é específico do Content Engine e não contém os contratos genéricos da Fase 2. |
| Migrations | Placeholder | `alembic/.gitkeep` é o único arquivo da área; não há configuração ou revisions Alembic. |
| Content Engine — A1 | Implementado | `src/pipeline/script_gen.py` contém geração em quatro chamadas, retry limitado, saneamento de saída e persistência local de roteiro/narração. `tests/pipeline/test_script_gen.py` cobre o comportamento com Ollama mockado. |
| Content Engine — mídia e publicação | Placeholder | `src/pipeline/tts.py`, `visuals.py`, `captions.py`, `assembly.py` e `upload.py` estão vazios. Nenhuma publicação real foi implementada. |
| Orquestração e filas | Placeholder | `src/core/celery_app.py` está vazio e `src/tasks/` contém somente `__init__.py` vazio. Não há workers, Beat, retries persistidos ou separação executável das filas `gpu`, `cpu` e `io`. |
| Control plane | Placeholder | `src/dashboard/main.py`, `src/dashboard/routers/__init__.py` e `src/dashboard/templates/.gitkeep` estão vazios. O console script inválido foi removido de `pyproject.toml`; um comando só deve ser publicado quando a aplicação FastAPI da Fase 3 existir. |
| Inteligência | Placeholder | `src/intelligence/trends.py`, `competitors.py` e `similarity.py` estão vazios. |
| Engajamento | Placeholder | `src/engagement/comments.py` está vazio. |
| Operações | Placeholder | `src/operations/health.py`, `seo.py` e `repurpose.py` estão vazios. |
| Receita e experimentos | Placeholder | `src/revenue/aggregator.py`, `profit.py` e `ab_testing.py` estão vazios; só os modelos de dados relacionados existem. |
| Extras e ações externas | Placeholder | `src/extras/affiliate.py`, `outreach.py` e `x_bot.py` estão vazios. Não há envio, gasto ou ação externa implementada. |
| Trading Research Lab | Planejado | O domínio aparece em `ARQUITETURA.md` e `ROADMAP.md`, mas não possui implementação. Trading com dinheiro real permanece fora do escopo. |
| Infraestrutura local | Parcial | `docker-compose.yml` define somente Redis com AOF. PostgreSQL, workers e health checks ainda não estão definidos. Ollama é esperado no host, fora do Compose. |
| Testes | Parcial | Há testes unitários para modelos e A1, incluindo falhas e retries mockados. Não há testes de Celery, dashboard, PostgreSQL, integrações reais, restart ou hardware. |

Arquivos `__init__.py` vazios são marcadores de pacote e não contam como funcionalidade.

## Comandos canônicos da Fase 1

Em um checkout limpo no Windows, sem instalar os extras pesados `ai`:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy src
docker compose config
```

O Redis local pode ser iniciado com `docker compose up -d redis`. Ainda não há comando válido
para executar a aplicação: `src/dashboard/main.py` é um placeholder. Essa lacuna não deve ser
mascarada por um comando que falha ou por antecipação da Fase 3.

## Baseline observado neste checkpoint

O ambiente fornecido possui Python 3.12.13. A primeira coleta, antes da instalação das
dependências do projeto, produziu:

- `python -m pytest -q`: interrompido na coleta por ausência de `sqlalchemy` e `httpx`;
- `python -m ruff check .`: não executado porque o módulo `ruff` não estava instalado;
- `python -m mypy src`: não executado porque o módulo `mypy` não estava instalado.

Após criar `.venv` e instalar `.[dev]` sem os extras `ai`:

- `python -m pip install -e ".[dev]"`: concluído; a primeira tentativa foi bloqueada pela rede da
  sandbox e a repetição com acesso autorizado concluiu a instalação;
- `python -m pytest -q`: **59 passed** em 1,73 s;
- `python -m ruff check .`: encontrou 7 ocorrências mecânicas preexistentes; o autofix removeu
  imports não usados, ordenou imports e simplificou um context manager; a repetição terminou com
  **All checks passed**;
- `python -m mypy src`: **Success: no issues found in 35 source files**;
- validação estrutural de `pyproject.toml` e `docker-compose.yml` por `tomllib` e YAML:
  concluída, incluindo metadata, ausência do console script placeholder e nome do container;
- `docker compose config`: bloqueado porque o executável Docker não está disponível neste
  ambiente.

## Verificações bloqueadas ou adiadas

- **Computador de casa:** confirmar Windows, CPU, RAM, disco e processo de startup/shutdown.
- **GPU:** confirmar RTX 3090, VRAM, CUDA, modelos, consumo e fila GPU com concorrência 1.
- **Serviços locais:** validar Ollama, Redis, PostgreSQL e recuperação após indisponibilidade.
- **Credenciais:** validar somente em modo opt-in as APIs oficiais de Google/YouTube, Reddit,
  bancos de mídia, X e demais provedores. Nenhuma credencial deve entrar no Git.
- **Ações externas:** publicação, mensagens, gastos, exclusões materiais e ações financeiras
  continuam sem implementação e exigem aprovação humana e kill switch antes de qualquer teste.

## Próxima fatia recomendada

Fechar o baseline instalando apenas as dependências core/dev, executar os 59 testes e as
verificações estáticas, corrigir somente falhas reproduzíveis e registrar os resultados. Depois,
o menor passo da Fase 2 é uma run manual persistida com estados explícitos, sem dashboard e sem
efeito externo.
