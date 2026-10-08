# Especificação — módulo `operations-brief`

**Status:** especificação da primeira fatia (execução manual). Escolhido na Fase 6 em
`docs/planning/phase-6-candidates.md`, com o tema "operação do próprio Automation Foundry".
**Contrato:** `$build-automation-module`.

## Resultado observável da primeira fatia

O operador abre o dashboard, escolhe uma janela (1, 7, 14 ou 30 dias) e inicia uma run. O
módulo lê somente o estado persistido, gera um brief em Markdown mais a evidência estruturada
que o sustenta, e abre uma approval com a página de revisão completa. Aprovar conclui a run
localmente; rejeitar a cancela. Nada é enviado, publicado ou gasto.

## Hipótese e métrica

- **Hipótese:** um brief operacional gerado do banco, com números rastreáveis, é aprovado sem
  reescrita e substitui a conferência manual das telas do dashboard.
- **Métrica de sucesso (proposta):** em 4 briefs semanais, ao menos 3 aprovados sem rejeição, com
  tempo de revisão menor que o baseline manual.
- **Baseline manual:** *pendente.* O proprietário ainda não informou quanto tempo leva hoje.
  A avaliação não pode concluir sem esse número.
- **Fora da primeira fatia:** schedule. O roadmap exige avaliar o experimento antes de automatizar.

## Contrato

| Item | Decisão |
|---|---|
| Slug / nome | `operations-brief` / "Operations Brief" |
| Código | `src/briefs/` (evidência, renderização, executor). Sem tabelas novas: usa o kernel. |
| Entrada | `window_days` ∈ {1, 7, 14, 30} e `window_end` (data UTC, fixada no parse do formulário). Tipada com Pydantic. |
| Saída | run `succeeded` com `output_payload` apontando step, artifacts e approval. |
| Trigger | manual pelo dashboard. Sem schedule nesta fatia. |
| Steps | 1. `build-operations-brief`, fila `io`: leitura do banco e escrita local. |
| Idempotência | chave do operador; step `{chave}:build-operations-brief`; a mesma chave com outra janela conflita. |
| Retry / timeout | no máximo 2 tentativas, backoff do wrapper, timeout limitado. Erros: `BRIEF_EVIDENCE_UNAVAILABLE` (retryable), `BRIEF_STORAGE_WRITE_FAILED` (retryable). |
| Cancelamento / kill switch | consultados antes de iniciar, como no A1; kill switch por automação já é do kernel. |
| Aprovação | `review_operations_brief`, ligada por digest aos IDs e SHA-256 dos dois artifacts. Aprovar conclui; rejeitar cancela. |
| Artifacts | `operations_brief_evidence` (JSON canônico) e `operations_brief` (Markdown), em `storage/operations-brief/run-<id>/`. Sensibilidade `internal`. Retenção de 180 dias, declarada em `retention_policy.py`. |
| Métricas | `operations_brief_runs_observed`, `operations_brief_failed_runs`, `operations_brief_markdown_bytes`. |
| Custo | entrada de ledger `cost`, categoria `external_api`, valor 0 BRL. Não há serviço externo. |
| Connector | uma `ConnectorObservation` para `platform-database` (estado, frescor da coleta e qualidade), o primeiro produtor real da tabela. |
| Segredos / serviços | nenhum. Sem rede, sem credencial, sem rate limit. |
| Saúde / degradação | banco indisponível → falha retryable e estruturada; janela sem atividade → brief válido que diz "sem atividade". |

## Evidência e redação

A evidência contém apenas contagens, slugs, IDs, estados, durações, idades, códigos de erro
permitidos por regex (`^[A-Z][A-Z0-9_]{2,63}$`, senão `UNKNOWN`) e totais decimais do ledger por
moeda. Ela **nunca** inclui payloads de entrada ou saída, mensagens de erro, textos de review,
tema de conteúdo, `source`, `category` ou `dimensions`, a mesma regra de redação do dashboard.
A própria run em andamento é excluída das contagens para o brief não se descrever.

Seções do brief: resumo; runs por automação (total, estados, taxa de sucesso, duração média);
falhas recentes com código; approvals pendentes com idade; alertas ativos; ledger por moeda;
connectors; limitações. O Markdown é função pura da evidência: o mesmo JSON gera o mesmo texto.

## Estados, falha parcial e repetição

- Entrega duplicada de run `awaiting_approval` ou `succeeded` reconfere os artifacts e devolve o
  resultado existente, sem reescrever arquivos.
- Falha ao registrar artifacts, métricas, ledger, connector ou approval acontece em savepoint e
  deixa a run `failed` com `BRIEF_EVIDENCE_PERSIST_FAILED`, retryable por uma nova run explícita.
- Artifact adulterado entre a geração e a aprovação (tamanho ou SHA-256) impede a página de
  revisão e a finalização, sem transicionar a run.
- Rejeição cancela pelo contrato compartilhado e preserva os arquivos para auditoria.

## Verificação planejada

Unitários: validação da entrada, redação, renderização determinística, janela vazia.
Integração (SQLite de teste): run completa, replay, falha de escrita, falha parcial, adulteração,
cancelamento, rejeição, approval com digest divergente. Guarda de retenção: os dois tipos novos
declarados. Smoke: execução real pelo dashboard e worker `io`, com a página de revisão aberta.
**Dependem do computador de casa:** apenas o smoke com PostgreSQL, Redis e workers reais.

## Segunda fatia (não implementada aqui)

Narrativa em linguagem natural via Ollama, atrás de adapter com fake, fila `gpu`. A narrativa não
poderá citar número que não esteja na evidência; esse é o teste de aceite. Só entra depois de
medir o valor do brief determinístico.
