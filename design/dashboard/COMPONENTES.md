# Componentes do painel — mapa para os templates Jinja

Folha de estilo e script: os mockups usam diretamente `src/dashboard/static/styles.css` (tokens + componentes) e `src/dashboard/static/theme.js` (opcional: tema, copiar hash, Esc fecha diálogo); não há cópia local, então qualquer ajuste de CSS aparece nos mockups e no painel ao mesmo tempo. Páginas: `visao-geral.html` (tela 1), `detalhe-run.html` (tela 2: run #10 aguardando approval, com polling), `detalhe-run-falha.html` (run falha, com tentativas e Retentar run) e `detalhe-run-conteudo.html` (run concluída com Export local, Resultado do conteúdo e Alinhamento da legenda). Tela 3: `revisao-brief.html`, `revisao-roteiro.html`, `revisao-roteiro-decidida.html` (somente leitura), `revisao-narracao.html`, `revisao-video.html` e `revisao-thumbnail.html`. Tela 4: `saude.html` (degradado) e `saude-falha.html` (falha, degradado e ignorado juntos). Tela 5: `storage.html`. Tela 6: `export-local.html`.
Tudo funciona sem JavaScript. Estado de uma entidade = **ícone + texto + cor** (`badge-<estado>` + ícone do sprite).

## Shell
| Classe | Quando usar |
|---|---|
| `.nav-toggle` + `.app` | Checkbox irmão anterior de `.app`; recolhe a sidebar (desktop) e abre a gaveta (mobile) sem JS. |
| `.sidebar`, `.sidebar-header`, `.workspace`, `.sidebar-content`, `.sidebar-footer` | Estrutura da sidebar. |
| `.nav-group` + `.nav-label` + `.nav-list` + `.nav-item` | Grupo com rótulo e itens. `aria-current="page"` marca o ativo. `.nav-count` leva o contador. |
| `.nav-sub` | Subitens recuados com linha vertical. |
| `.topbar`, `.sidebar-toggle`, `.breadcrumb`, `.theme-toggle` | Barra superior. |
| `.page`, `.page-header`, `.page-actions`, `.section`, `.section-head` | Miolo da página e cabeçalhos de seção. |
| `.grid`, `.grid-kpi`, `.grid-cards`, `.grid-2`, `.split` | Grades. `.split` = coluna larga + coluna lateral (vira 1 coluna < 64rem). |

## Estados
| Classe | Valores |
|---|---|
| `.badge.badge-<x>` | Run/step: `queued` `running` `awaiting_approval` `succeeded` `failed` `cancelled`. Alerta: `info` `warning` `error` `critical` (+ `danger`). Saúde: `pass` `degraded` `fail` `skip`. Connector: `healthy` `degraded` `unavailable` `disabled`; qualidade `pass` `warning` `fail` `unknown`; frescor `fresh` `stale`. Approval: `pending` `approved` `rejected`. Genéricos: `success` `warning` `danger` `info` `neutral` `secondary`. |
| `.dot`, `.dot-success/-warning/-danger/-info/-live` | Indicador mínimo (rodapé da sidebar, "ao vivo"). Nunca sozinho: sempre com texto. |
| `.live` | Rótulo "ao vivo / atualiza a cada N s" ao lado de `.dot-live`. |

Ícones sugeridos: succeeded `circle-check` · failed `circle-x` · cancelled `ban` · queued/awaiting `clock` · running `loader` (gira) · warning `triangle-alert` · kill switch `octagon-x` · desabilitada `power`.

## Cards
| Classe | Quando usar |
|---|---|
| `.card` + `.card-header` / `.card-title` / `.card-description` / `.card-content` / `.card-footer` | Container padrão. `.card-header.row` coloca ação à direita; `.card-content.flush` remove padding (tabelas/listas); `.card-footer.plain` sem borda. |
| `.kpi` + `.metric` + `.kpi-foot` | Cartão de métrica (use `<a class="card kpi">` quando leva a outra seção). |
| `.spark` | Mini barras monocromáticas; a última recebe o acento. Sempre com `role="img"` e `aria-label`. |
| `.card-select` | Card clicável e marcável (thumbnails): `<label class="card card-select"><input type="radio" name=…><span class="check">✓</span>…</label>`. Selecionado = borda escura. |
| `.automation` (+ `.is-killed`) | Card de automação: título, chips de estado, `.stat-row`, rodapé com **Executar**. |
| `.attention` | Faixa "Precisa de você". Vazio: `.attention.is-clear` + `.empty.is-good`. |

## Alertas e vazios
| Classe | Quando usar |
|---|---|
| `.alert` + `.alert-icon` / `.alert-body` / `.alert-title` / `.alert-description` / `.alert-action` | Linha de aviso com ação. Variantes: `.alert-destructive` (error, critical), `.alert-warning`, `.alert-info` (ação humana pedida), `.alert-success` (notice). |
| `.empty` (+ `.is-good`) | Estado vazio. `.is-good` = vazio positivo ("Nenhuma aprovação pendente"). |

## Dados
| Classe | Quando usar |
|---|---|
| `.table-wrap` > `.table` | Tabela; rola dentro do card. `th scope`, `caption.sr-only`. `.num` alinha números à direita. `small` na célula = linha secundária. |
| `.list` | Lista de itens com ícone, texto e badge (schedules, connectors). |
| `.progress` (+ `.is-warning` `.is-danger` `.is-brand`) | Barra de uso. Defina `style="--value:88%"` e `role="progressbar"`. Regra sugerida: ≥ 80 % warning, ≥ 95 % danger. |
| `.meter` / `.meters` | Linha rótulo + barra + valor. |
| `.timeline` + `.timeline-dot` | Steps da run (tela 2). |
| `.hash` | SHA-256 truncado + botão `data-copy="<hash completo>"` (tela 3). |

## Formulários (sempre `POST`, com `csrf_token`)
| Classe | Quando usar |
|---|---|
| `.field` + `label[for]` + `.hint` | Campo com rótulo e ajuda. |
| `.input`, `.textarea`, `.select` | Controles de texto. |
| `.check-row` + `.checkbox` | Caixa de confirmação obrigatória. |
| `.switch` | Alternância (só quando a ação é reversível e sem motivo obrigatório). |
| `.form-stack` | Empilha campos com espaçamento. |

## Botões
`.btn` (primário) · `.btn-secondary` · `.btn-outline` · `.btn-ghost` · `.btn-destructive` · `.btn-success` (Aprovar) · tamanhos `.btn-sm`, `.btn-icon`.
Rótulos literais preservados: **Executar módulo**, **Desabilitar módulo**, **Ativar kill switch**, **Solicitar cancelamento**, **Retentar run**, **Aprovar approval**, **Rejeitar approval**, **Decisão registrada**.

## Sobreposições (sem JS)
| Classe | Quando usar |
|---|---|
| `.menu` (`<details>`) + `.menu-content` + `.menu-item` (+ `.danger`) | Menu de ações recolhidas. |
| `.dialog-backdrop#id` + `.dialog` | Diálogo aberto por `<a href="#id">`; fecha com `<a href="#">`. Um por módulo. |
| `.danger-zone` + `.disclosure` | Zona de perigo em vermelho; cada ação é um `<details>` com confirmação. |
| `.tooltip` + `[role=tooltip]` | Valor completo (hash, UTC) em hover/foco. |
| `.kbd` / `<kbd>` | Atalhos. |
| `.tabs` + `.tab[aria-selected]` | Abas como links para seções/rotas (tela 2). |

## Detalhe da run (tela 2)
| Classe | Quando usar |
|---|---|
| `.title-row` | `h1` + badge de status lado a lado. |
| `.facts` > `.fact` > `dt`/`dd` | Faixa de fatos (status, trigger, duração, enfileirada; indicadores de conteúdo; alinhamento da legenda). |
| `.timeline` > `li` > `.timeline-dot.is-success/-danger/-warning/-info/-neutral` + `.timeline-item` | Steps em ordem. O ponto leva o ícone do estado. Itens que não são steps (ex.: "Aprovação humana") usam o mesmo padrão. |
| `.timeline-head`, `.timeline-meta` | Nome do step + badge; fila, nº de tentativas e duração. |
| `.attempts` > `li` (+ `.is-failed`) | Lista de tentativas de um step; só renderize se houver mais de uma tentativa ou falha. Mostra código de falha **redigido** + tipo da exceção. |
| `.tabset` > `input[type=radio]` × N + `.tablist` + `.tab-panels > .tab-panel` | Abas sem JS (até 7), com setas ← → nativas. Renderize só abas com conteúdo; `.count.is-attn` destaca pendência. |
| `.kv` (`dt`/`dd`) | Chave → valor (contexto seguro de revisão). |
| `.hash` + `.tooltip` + `.copy-btn[data-copy]` | Hash truncado com valor inteiro no tooltip e botão copiar. |
| `.btn-outline-danger` | Ação destrutiva reversível só por nova ação (Solicitar cancelamento) no cabeçalho. |

Regras desta tela: **um lugar por ação** — Retentar run e Solicitar cancelamento só no cabeçalho (abrem diálogo com `csrf_token`, motivo e confirmação); decidir approval só em `/approvals/{id}/review` (a run só mostra o contexto e o link). O polling de 2 s só existe enquanto a run não é terminal; ao terminar, o selo "Ao vivo" vira "Finalizada".

## Revisão de approval (tela 3)
| Classe | Quando usar |
|---|---|
| `.review-layout` > `.review-content` + `.decision-panel` | Duas colunas: evidências à esquerda, painel de decisão fixo (`position: sticky`) à direita; vira 1 coluna < 64rem com o painel depois do conteúdo. |
| `.verify-strip` | Faixa verde "Integridade verificada" com os hashes (`.hashes` > `span` > `.hash`). Sempre no topo do conteúdo. |
| `.prose` / `.prose-mono` | Texto a revisar: roteiro em fonte de sistema, Markdown do brief em monoespaçada (`pre`, `white-space: pre-wrap`). Conteúdo sempre escapado pelo Jinja. |
| `.player` + `.player-split` (+ `.is-wide`) | `<audio controls>` / `<video controls>` locais; `.player-split` põe o vídeo vertical ao lado dos metadados. |
| `.gauge` (+ `.limit`) | Barra com rótulo e valor; `.limit` marca a tolerância/limite de bloqueio (`--limit:35%`). Usar em sincronismo A4 e originalidade A6. |
| `.kv`, `.table` | Metadados e comparações históricas. Vazio: `.empty` "Nenhuma run anterior aprovada entrou na janela de comparação." |
| `.immutable` | Aviso âmbar com cadeado dentro do painel: "A decisão é imutável" + o efeito real da aprovação. |
| `.decision-choice` (`#dec-approve`/`#dec-reject` + `.choices` + `.decision-forms` > `#form-approve`/`#form-reject`) | Seletor sem JS que revela um formulário por vez. **Cada formulário continua sendo um POST separado** (`/approvals/{id}/approve` e `/reject`), com `csrf_token`, `actor`, `reason`, `confirmation` (`approve-approval` / `reject-approval`). |
| `.decision-record` + `.seal` + `.readonly-banner` | Estado "Decisão registrada": sem formulários, selo com cadeado, badge da decisão e link de volta à run. |
| `.thumb-grid` > `label.card.card-select.thumb` | Escolha de thumbnail: card inteiro clicável, borda escura + check no canto quando selecionado. O radio usa `form="form-approve"` para enviar `thumbnail_artifact_id` junto com o painel. Exatamente um (`required`). |

Regras: o conteúdo a decidir nunca mostra caminhos de arquivo; mídia vem de `/approvals/{id}/…` locais. Rótulos preservados: "Aprovar approval", "Rejeitar approval", "Aprovar thumbnail selecionada", "Rejeitar approval A8", "Decisão registrada". Na thumbnail a rejeição avisa que **cancela a run**.

## Saúde operacional (tela 4)
| Classe | Quando usar |
|---|---|
| `.health-card` (+ `.is-pass` `.is-degraded` `.is-fail` `.is-skip`) | Card de um probe (`report.checks[*]`). A classe de estado muda a borda; falha também tinge o fundo. |
| `.metric-row` | Métrica grande à esquerda e badge de estado à direita (o título nunca disputa espaço com o badge). |
| `.progress` (+ `.is-warning` ≥ 80 %, `.is-danger` ≥ 95 %) | Uso de CPU, memória, disco e VRAM. Probes sem percentual (banco, Redis, Beat) mostram latência ou estado como métrica. |
| `.health-foot` | Pares rótulo/valor no rodapé (`metrics` seguras: `free_gib`, `latency_ms`, `memory_used_mib`…). |
| `.queue-grid` > `.queue-cell` | Workers por fila (`gpu`, `cpu`, `io`). |
| `.summary-chips` | Contagem por estado (operacionais, degradados, com falha, ignorados). |
| `.raw-probes` (`<details>`) | Tabela recolhida com todos os probes e métricas brutas já redigidas. |

Estados: `pass` → "Operacional" (`circle-check`), `degraded` → "Degradado" (`triangle-alert`), `fail` → "Falha" (`circle-x`), `skip` → "Ignorado" (`minus-circle`). O aviso do topo é `.alert-warning` (degradado), `.alert-destructive` com `role="alert"` (falha) ou `.alert-success` (tudo operacional). "Verificar novamente" é um link `GET /health`; a tela não faz polling.

## Storage e retenção (tela 5)
| Classe | Quando usar |
|---|---|
| `.class-card` (+ `.is-success` `.is-info` `.is-warning` `.is-danger` `.is-neutral` `.is-zero`) | Um card por classe de retenção. O tom segue a classe: `retained` success, `retention_hold` info, `expired` e `orphan` warning, `missing` e `unsafe` danger, `purged` neutral. Classe com 0 itens vira `.is-zero` (esmaecida, sem cor). |
| `.grid-class` | Grade dos 7 cards (4 por linha em desktop). |
| `.stack` + `.seg-<classe>` + `.legend` | Barra empilhada da distribuição por bytes, com legenda textual. `role="img"` e `aria-label` com os valores. |
| `abbr[title]` | Tamanho legível ("11,4 GiB") com o valor exato em bytes no `title`. |
| `.facts` | Resumo da varredura (gerada em, varridos, excluídos, apagados). |
| `.table` em `.card-content.flush` | "Por automação" e "Política de retenção" (tipo, prazo em badge, produtor, justificativa). |

Regras: tela estritamente somente leitura (sem formulários, sem `csrf_token`); nenhum caminho de arquivo é exibido; o aviso vermelho do topo só aparece se houver itens `missing` ou `unsafe`. Estados de exceção (`error`, raiz ausente) estão em comentário no HTML.

## Export local (tela 6)
| Classe | Quando usar |
|---|---|
| `.verify-strip` + `.check-list` | Faixa "Reconferido agora" com a lista do que foi reverificado (approvals A7/A8, tamanho e SHA-256). |
| `.file-card` | Um card por arquivo do pacote (papel, artefato, tamanho, formato, SHA-256 truncado com copiar, botão de download). Thumbnail é opcional: sem ela, renderize só o card do vídeo. |
| `.btn[download]` | Download de vídeo (`/local-export/video`), thumbnail (`/local-export/thumbnail`) e manifesto (`/local-export/manifest.json`). São `GET`; a tela não tem formulários. |

## Como migrar para os templates Jinja
1. (Feito) `styles.css` e `theme.js` já vivem em `src/dashboard/static/` e são servidos em `/static/…` junto com o HTMX. Nenhum recurso externo é usado.
2. Extraia o shell (`<aside class="sidebar">`, `.topbar`, sprite de ícones) para um `base.html` e o sprite para `_icons.html`; as telas viram `{% extends %}`.
3. O sprite SVG é único por página (`<symbol id="i-…">`); use `<svg class="icon"><use href="#i-nome"/></svg>`. Os mockups geram o sprite completo em todas as páginas; em produção inclua só uma vez no `base.html`.
4. Cada comentário `<!-- DINÂMICO … -->` marca o dado ou o loop Jinja; `<!-- ÁREA AUTO-ATUALIZÁVEL -->` marca os dois fragmentos HTMX (`#data-observability`, `#run-live-status`).
5. Ordem sugerida: `base.html` + Visão geral → Detalhe da run → Revisão de approval → Saúde → Storage → Export local. Mantenha os rótulos literais de ação (lista na seção "Botões").
6. Os valores dos mockups (hashes, durações, nomes de step do Content Engine, latências) são ilustrativos; os mapeamentos reais estão nos comentários.

## Pontos de integração HTMX (marcados nos comentários do HTML)
- `#data-observability`: `hx-get="/data-observability/fragment" hx-trigger="every 30s" hx-swap="outerHTML" aria-live="polite"`.
- Tela da run (próxima): `#run-live-status` com `hx-trigger="every 2s"` apenas enquanto a run não é terminal, `aria-live="polite"`.

## Observação sobre os dados de exemplo
As runs #11 (falha) e #5 (passos, artefatos, indicadores) têm detalhes ilustrativos; nomes de step do Content Engine (`generate-script-a1`…) e códigos de falha (`ollama_unavailable`) são exemplos. A run #10 aparece como **Aguardando aprovação** (e não "concluída") porque existe uma approval pendente para ela; os demais valores seguem a lista fornecida. Horários/durações não informados (ex.: duração do #5) são ilustrativos.

## Como visualizar os mockups
Abra qualquer `.html` desta pasta direto no navegador (`file://`), ou sirva a **raiz do repositório** para que o caminho relativo até `src/dashboard/static/` resolva:

```bash
python -m http.server 8765 --bind 127.0.0.1
# depois abra http://127.0.0.1:8765/design/dashboard/visao-geral.html
```

Servir apenas `design/dashboard/` não funciona: o CSS fica fora dessa pasta.
