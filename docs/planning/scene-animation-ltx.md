# Plano: cenas animadas por IA com LTX-2.3 (proposta)

Proposta de 2026-10-15, a pedido do proprietário, depois do teste fora da pipeline em
`D:\automation-foundry\labs\video-wan22-i2v\` (números e scripts no README de lá). A fatia 1
(step A5a) foi juntada em 2026-10-15, desligada por padrão; as fatias 2 e 3 (o A5 lendo o manifesto e o `ExplainerV1` tocando os clipes) também, em 2026-10-15. As decisões do proprietário estão na seção 7; os
contratos da seção 3 foram escritos com testes na fatia 0 (2026-10-15).

## 1. Resultado esperado

Cada imagem aprovada no gate do A3 vira um clipe curto com movimento natural (pessoa
andando, mão pegando um objeto, câmera se aproximando), gerado localmente pelo LTX-2.3. O
A5 continua montando o vídeo pelo Remotion, com as mesmas legendas e sobreposições do
`explainer_v1`, mas cada cena passa a tocar o clipe em vez da imagem com zoom.

Medido no teste (run 19, 9 cenas, 50 s, RTX 3090): 14,4 min de GPU por vídeo, ~75 s por
cena de 5,5 s, 704×1280 nativo ampliado para 1080×1920.

## 2. Onde entra na pipeline

```text
A3 imagens → gate A3 → A4 legendas → [A5a cenas animadas] → A5 montagem → A6 → A7 → A8
```

- **Step novo `animate-scenes-a5a`**, na fila `gpu`, depois do A4 e antes do A5. Ele vem
  depois do A4 porque a duração de cada cena só é conhecida com as palavras da legenda: o
  corte entre cenas é o início da primeira palavra do trecho de cada imagem
  (`timeline_builder._clips`). Esse cálculo passa a ser uma função pública usada pelo A5a e
  pelo A5, e o A5 recusa a montagem se os cortes não forem os mesmos com que os clipes foram
  gerados.
- **Por que um step separado e não dentro do adapter do A5**: são ~15 min de GPU por vídeo,
  com uma falha possível por cena. Separado, cada clipe pronto vira artifact conferido por
  SHA-256 e uma nova tentativa só refaz o que falta; o A5 continua curto e igual.
- **Sem o A5a** (backend `disabled`, o padrão) nada muda: o A5 usa as imagens como hoje.

## 3. Contratos novos ou alterados

1. **Movimento por cena.** O modelo de texto escreve uma frase de movimento por cena, em
   inglês, a partir da descrição da cena aprovada e do trecho narrado. É conteúdo, não
   tempo: a duração vem do áudio (decisão 11 da arquitetura). O código confere tamanho,
   idioma e proibições (texto, letras, logotipo, tela visível, pessoa nova) e devolve o
   motivo da recusa como no plano de cenas; recusada três vezes, a cena usa um movimento
   neutro fixo ("subtle natural movement, slow camera push-in").
2. **Manifesto de cenas animadas** (`scene_clips_manifest`): por cena, índice, imagem e
   SHA-256 de origem, início e fim em ms, frase de movimento, seed, quadros, fps, arquivo e
   SHA-256 do clipe, ou `fallback: still` com o motivo. Também guarda o digest dos cortes,
   o inventário dos modelos e a versão do grafo.
3. **Timeline v3** (a v2 proíbe campos a mais): cada `clip` aceita `video` + `video_sha256`
   e, nesse caso, `effect` precisa ser `none`. O resto da v2 fica igual. Mudar o contrato é
   decisão da integradora, e a faixa C precisa desenhar o vídeo (`<OffthreadVideo>`) em vez
   da imagem no `ExplainerV1`.
4. **Duração do clipe**: trecho da cena mais metade do crossfade em cada lado, em 25 fps e
   com 8k+1 quadros (exigência do LTX), limitado a 8 s. Cena mais longa: o clipe é esticado
   até 1,25× e, acima disso, a cena cai para a imagem com zoom. O clipe de 9 s chegou a
   63,7 GB de RAM numa máquina de 64 GB.

## 4. Runtime do modelo

- **ComfyUI como ferramenta de render**, igual ao Remotion e ao FFmpeg: versão fixada
  (v0.40.0), ambiente virtual próprio no D:, sem nós de terceiros, iniciado pelo step em
  `127.0.0.1` numa porta livre e encerrado no fim da tentativa, com a árvore de processos
  morta em timeout como já faz o A5 com o Remotion. Não é um serviço novo nem fica no ar
  entre runs. O grafo é montado em código (o mesmo do template oficial `video_ltx2_3_i2v`
  usado no teste), e não lido de arquivo.
- **Pesos conferidos antes de iniciar**: inventário SHA-256 do checkpoint destilado fp8, do
  upscaler e do Gemma 3 fp4 (como o adapter do FLUX.2 klein já faz), sem download implícito.
- **Memória**: antes de subir o ComfyUI, libera o modelo de texto local (como o A5 já faz) e
  exige um mínimo de RAM livre configurável; abaixo disso a tentativa falha como retryable,
  sem iniciar a geração.
- **Velocidade**: SageAttention e acumulação fp16 entram como opções fixadas do runtime
  (wheel do Sage com SHA-256 registrado).
- Alternativa descartada por agora: rodar o LTX pelo `diffusers` num subprocess, como o
  FLUX.2 klein. Evita o ComfyUI, mas o pipeline de duas etapas do LTX-2.3 não foi testado
  nesse caminho.

## 5. Falhas, idempotência e observabilidade

- Chave de cada clipe: SHA-256 da imagem, frase de movimento, seed, quadros, inventário dos
  modelos e versão do grafo. Clipe já gerado e conferido não é refeito; regenerar exige uma
  run nova ou uma ação explícita.
- Por cena: timeout próprio. Falha permanente vira `fallback: still` registrado (a cena
  fica com zoom) e o vídeo segue; o step só falha se nenhuma cena puder ser animada. Falha
  de infraestrutura (ComfyUI não sobe, RAM insuficiente) é retryable e reaproveita os clipes
  prontos.
- Métricas por clipe e por step: segundos de GPU, pico de VRAM e RAM, quadros, cenas
  animadas e cenas que caíram para imagem, energia estimada e custo externo zero.
- O limite atual da tarefa Celery é 900/960 s, e um vídeo de 9 cenas levou 864 s só de
  geração. Decidido (seção 7): o step não começa um clipe que não cabe no orçamento que
  sobra da tentativa, publica os prontos e termina com uma falha retryable
  `CONTENT_SCENE_ANIMATION_BUDGET_EXHAUSTED`; a próxima tentativa reaproveita os clipes
  conferidos e continua. Os retries desse código não contam como falha de infraestrutura,
  mas têm um teto de tentativas para um vídeo que nunca termina falhar fechado.

## 6. Licença e escopo

- LTX-2 Community License: gratuita para quem fatura menos de US$ 10 milhões por ano, com
  lista de usos proibidos. Aceita pelo proprietário em 2026-10-15
  (`docs/licensing/content-video.md`).
- O clipe herda o escopo da imagem de origem: enquanto as imagens do FLUX.2 klein forem
  `quality-test-only`, os clipes também são, e o rótulo segue até o export.
- ComfyUI (GPL-3.0) é usado como ferramenta local, sem redistribuição; SageAttention
  (Apache-2.0) e triton-windows (MIT) também.
- LTX-2.5 (pesos INT8 nativos, provavelmente mais rápido) exige aceitar a licença no Hugging
  Face com a conta do proprietário; espelhos sem essa restrição não são usados.

## 7. Decisões do proprietário (2026-10-15)

1. **Licença**: aceita, registrada em `docs/licensing/content-video.md`.
2. **Tempo de tarefa**: o limite da fila `gpu` não muda. O step para quando o orçamento da
   tentativa acaba e a próxima tentativa continua dos clipes prontos; nada no `.env` nem
   reinício de workers.
3. **Revisão**: gate de clipes no mesmo estilo da revisão de imagens (seção 8).
4. **Cenas acima de 8 s**: clipe esticado até 1,25× e, acima disso, a cena fica com a
   imagem e zoom.

## 8. Gate de clipes

Mesmo desenho das rodadas de rascunho das imagens (`visual_drafts.py` e
`visual_review.py`), com clipes no lugar das imagens:

- Cada rodada é um step `gpu` próprio (`draft-scenes-a5a-rN`) com manifesto e clipes
  registrados; a approval `review_content_scene_clips_a5a` fica ligada ao manifesto e ao
  SHA-256 de cada clipe.
- A página `no-store` mostra cada clipe tocando ao lado do trecho narrado, da imagem de
  origem e da frase de movimento usada; os MP4 são servidos por rota própria só depois de
  reconferir o SHA-256, com `nosniff` e Range, como o vídeo do A7.
- O operador aprova, rejeita ou marca clipes com um comentário (`choice=redo` e uma nota
  por clipe). A rodada seguinte reescreve só essas frases de movimento a partir da nota,
  gera só esses clipes e copia os demais conferidos por SHA-256.
- Uma cena também pode ser marcada para ficar com a imagem e zoom, sem gerar de novo.
- Só a rodada aprovada segue para o A5; rejeitar cancela a run.
- Com o gate, o step A5a passa a ser a primeira rodada; sem o gate (padrão desligado), a
  rodada única segue direto para o A5.

## 9. Fatias e faixas

| Fatia | Faixa | O que entrega |
| --- | --- | --- |
| 0 | Integradora | **Feita em 2026-10-15**: contratos da seção 3 com testes (seção 6 de `docs/reference/content-contracts-v1.md`), decisão 14 na arquitetura, faixa E em `lanes.md` |
| 1 | E (nova, Claude) | **Juntada em 2026-10-15** (merge `e7d5dca`), desligada por padrão e sem geração real ainda: `src/pipeline/motion_generation.py` (pede as frases ao modelo de texto e reenvia o motivo da recusa), `src/pipeline/scene_animation.py` (step A5a, orçamento por tentativa, cache, fallback, métricas), `src/pipeline/comfyui_runtime.py` com fake, config e `.env.example` |
| 2 | C (Codex) | **Juntada em 2026-10-15** (merge `3f89ae1`, revisada pela sessão integradora): `ExplainerV1` aceita timeline v2 e v3 e toca o clipe com `<OffthreadVideo>` mudo na janela do clipe; conferido com um quadro parado e MP4 sintético, sem render completo |
| 3 | D (Claude) | **Juntada em 2026-10-15** (merge `af4f055`, revisada pelo Codex em duas passadas): com o A5a ligado, o A5 confere o manifesto, os cortes e os MP4 e monta o timeline v3, aceitando clipes de tentativas anteriores do A5a; com o A5a desligado, segue no timeline v2 |
| 4 | E (Claude) | **Juntada em 2026-10-15** (merge `754bec5`, revisada pela sessão integradora), desligada por padrão: gate de clipes da seção 8 com rodadas `draft-scenes-a5a-rN`, approval `review_content_scene_clips_a5a`, página e rotas dos MP4 com SHA-256 reconferido; a página só foi vista com clipes falsos, sem tocar vídeo de verdade |
| 5 | Integradora | Verificação de preparação **juntada em 2026-10-15** (merge `2382eb5`): ComfyUI e venv, tamanhos exatos dos três pesos, RAM livre e Remotion fixado, sem rodar modelo. Faltam o detalhe da run e o teste opt-in real |
| 6 | Proprietário + integradora | Uma run real A1→A7 com LTX na RTX 3090, medindo tempo, RAM e qualidade |

As fatias 1 e 2 podem correr em paralelo depois da fatia 0; a 3 depende das duas; a 4
depende da 1. A página do gate edita templates do painel, então segue a regra das faixas:
worktree e servidor de demonstração em outra porta com publisher falso.
