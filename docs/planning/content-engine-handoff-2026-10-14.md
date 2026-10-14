# Content Engine — passagem de bastão (2026-10-14)

Para continuar em outra sessão. Leia antes `AGENTS.md`, `ARQUITETURA.md`, `ROADMAP.md` e a
skill `.agents/skills/build-content-engine/SKILL.md`. Este documento substitui o de
2026-10-10 como ponto de partida; o histórico detalhado está no `ROADMAP.md`.

## Foco agora

Roteiro (A1) e narração (A2) estão dados por concluídos pelo proprietário. O foco é:

1. **Imagens de qualidade que façam sentido entre si.** Na run 16 as nove descrições de
   cena ficaram repetitivas (mesa de quarto, caderno, "figura de costas" em quase todas): a
   regra "não repita a cena" não foi seguida e o pedido de mostrar "alguém de costas" foi
   copiado literalmente. Cada imagem é descrita sozinha; não há um plano de cenas do vídeo
   inteiro nem consistência de personagem ou cenário entre imagens.
2. **Edição.** Hoje é a edição `motion` do FFmpeg (zoom/deslocamento lento, crossfade de
   0,4 s, cortes no início das frases).

## Como trabalhar com o proprietário

- Não é técnico em mídia/ML. Explique simples, recomende uma opção e peça a decisão.
- Julga ouvindo, assistindo e lendo: entregue arquivos para comparar com caminho absoluto.
  Há duas pastas `storage`: a do projeto (`C:\...\automation-foundry\storage`, experimentos)
  e a do pipeline (`D:\automation-foundry\storage`).
- Peça autorização antes de baixar modelos, reiniciar o runtime, gastar, publicar ou apagar.
  Autorizações valem caso a caso.
- Commits pequenos, sem atribuição de IA, na branch `chore/project-skills`. Push só quando
  ele pedir. Não fazer merge em `main` nem abrir PR sem pedido explícito.

## Estado desta máquina

- `.env`: `CONTENT_VISUAL_BACKEND=flux2_klein_quality_test`, `CONTENT_VISUAL_REVIEW_ENABLED=true`,
  caminhos do ambiente separado em `D:\automation-foundry\labs\img-flux2-klein\`,
  `CELERY_SOFT_TIME_LIMIT_SECONDS=900`, `CELERY_HARD_TIME_LIMIT_SECONDS=960`,
  `CELERY_DISPATCH_LEASE_SECONDS=1200` (a lease precisa exceder o limite rígido, senão os
  workers não sobem), `CONTENT_QUICK_TOPICS_PATH=./imports/content-pautas.txt` e
  `CONTENT_DEFAULT_PERSONA`.
- Runtime: `automation-foundry-runtime stop`, esperar a tarefa `AutomationFoundryRuntime`
  sair de `Running`, depois `Start-ScheduledTask -TaskName AutomationFoundryRuntime`. Conferir
  antes que nenhuma run está executando. O painel em `127.0.0.1:8000` recarrega templates do
  disco mas mantém o Python antigo até reiniciar.

## O que existe para imagens

- `src/pipeline/visual_prompts.py`: descrição de cena por trecho (modelo do roteiro, API
  nativa do Ollama com contexto 8.192), checagens em código, três estilos (`photo`,
  `cartoon`, `dark`), reescrita a partir do comentário do revisor.
- `src/pipeline/visual_generation.py`: adapter `flux2_klein_quality_test` (FLUX.2 klein 4B,
  inventário SHA-256 da pasta do modelo, subprocess offline, `commercial_use=False`).
- `src/pipeline/visual_drafts.py` + `visual_review.py`: rodadas de rascunho revisadas no gate
  A3; o operador aprova, rejeita ou devolve imagens com comentário (até 7 por rodada, sem
  limite de rodadas). Só a rodada aprovada entra no A3.
- Escopo `quality-test-only` em tudo que usa imagem gerada, até o aceite escrito dos riscos
  do modelo (dados de treino não divulgados, titularidade do output, usos proibidos).
- Experimentos fora do pipeline: `scripts/experiments/e9_visual_prompts.py` e
  `flux2_klein_runner.py` geram folhas de contato por estilo.

## Pendências conhecidas

- Run 17 (primeira com rodadas de rascunho) falhou no gate de alinhamento com
  `BLOCK_ONSET_OFFSET` (bloco "3." 0,68 s fora da retomada de fala). A causa está no texto
  narrado, não na régua: a narração aprovada trouxe numeração de lista ("1.", "2.", "3."),
  abreviação ("Ex:") e aspas soltas. Como a legenda copia a grafia do roteiro, apareceram
  blocos só com "1.", "2.", "3." e "?’", e o trecho "Ex: organização da mesa." ficou sem
  legenda (a transcrição ouviu outra coisa e o trecho sem correspondência é ocultado).
  **Corrigido no código em 2026-10-14** (`_extract_narration` em `script_gen.py` e
  `_screen_words` em `captions.py`): "1." no início da linha vira "Primeiro," (até
  "Décimo,"; acima disso, hífen e bolinha somem), "Ex:"/"Ex.:" vira "Por exemplo,", aspas
  sem par na mesma linha somem; na legenda, número de lista que abre frase aparece com a
  palavra seguinte e pontuação solta cede o tempo à palavra anterior. Pendente: reiniciar
  o runtime (os workers mantêm o Python antigo) e conferir o gate na próxima run real.
- No detalhe da run, os steps são agrupados por ordinal: `draft-visuals-a3-r1` e
  `prepare-visuals-a3` (ambos ordinal 3) aparecem como duas "Tentativa 1" do mesmo step.
- Aceite escrito do proprietário para uso monetizado das imagens geradas.
- API paga de imagem: decisão D8 ("só se muito necessário e muito custo-benefício"); nenhum
  provedor foi avaliado na fonte.
- A checagem de descrições é um filtro por palavras; não reconhece pessoa real nem marca.
- Formato long nunca validado em nenhuma etapa.
