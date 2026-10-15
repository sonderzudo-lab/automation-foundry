# Faixas de trabalho em paralelo (piloto multi-agente)

Combinado com o proprietário em 2026-10-14. Vários agentes (Claude Code, Codex, Cursor) podem
trabalhar ao mesmo tempo, desde que cada um tenha uma faixa própria. Todos seguem o
`AGENTS.md` e as skills de `.agents/skills/`; este arquivo só divide o trabalho.

## Regras

1. **Contrato antes da faixa.** Uma faixa só começa depois que os contratos que ela usa estão
   commitados na branch de integração (`chore/project-skills`). Hoje:
   `docs/reference/content-contracts-v1.md`. Mudar um contrato é decisão da sessão
   integradora, não de uma faixa.
2. **Um escritor por arquivo.** Cada faixa trabalha no próprio worktree e branch e só altera
   os arquivos listados abaixo. Precisou mexer em outro? Pare e peça à integradora.
3. **Nada compartilhado.** Nenhuma faixa altera `.env`, reinicia o runtime ou os workers,
   edita templates no checkout principal (o painel em `127.0.0.1:8000` recarrega templates do
   disco) ou publica no broker Celery real. Servidor de demonstração usa outra porta e
   publisher falso.
4. **GPU é uma só.** A fila `gpu` tem concorrência 1. Testes de faixa usam fakes; geração real
   (FLUX, TTS, Whisper, Ollama) só com combinação prévia e uma faixa por vez.
5. **Pronto para integrar** quando `pytest` passa no worktree da faixa e a outra ferramenta
   revisou o diff (Codex revisa diff do Claude; `/code-review` revisa diff do Codex).
6. **Integração uma faixa por vez**, com a suíte completa verde depois de cada merge.

## Faixas atuais

| Faixa | Ferramenta | Branch | Pode alterar |
| --- | --- | --- | --- |
| A — modelo de texto e plano de cenas (etapas 1–3) | Claude Code | `feat/text-model-provider` | **juntada em 2026-10-14** |
| B — teste do Remotion (etapa 4, fora da pipeline) | Codex | `spike/remotion-timeline` | **substituída** pela demo em `D:\automation-foundry\labs\remotion-html-demo\` |
| C — modelos de animação no Remotion (etapa 4) | Codex | `feat/remotion-explainer` | só `render/remotion/**` |
| D — plano de animação, timeline v2 e backend A5 (etapa 4) | Claude Code | `claude/animated-assembly-fc5846` — **juntada em 2026-10-14** | `src/pipeline/animation_generation.py` e `src/pipeline/timeline_builder.py` (novos), `src/pipeline/assembly.py` (só o adapter novo), `src/pipeline/a1_executor.py` (só a escolha de fila e adapter do A5), `src/core/config.py` (só campos novos), `.env.example`, testes correspondentes |
| E — cenas animadas por IA com LTX-2.3 (etapa 4b, fatias 1 e 4) | Claude Code | `feat/scene-animation` | `src/pipeline/motion_generation.py`, `src/pipeline/scene_animation.py` e `src/pipeline/comfyui_runtime.py` (novos), `src/pipeline/a1_executor.py` (só a escolha do step A5a e do seu adapter), `src/core/config.py` (só campos novos), `.env.example`, testes correspondentes; na fatia 4, também a página e a rota do gate de clipes |
| Integradora | proprietário + sessão principal | `chore/project-skills` | contratos, docs, merges |

### Fronteira entre as faixas C e D

As duas faixas só se encontram nesta chamada, que nenhuma delas pode mudar sozinha. O A5
(`remotion_quality_test` em `src/pipeline/assembly.py`) a executa com `render/remotion` como
pasta de trabalho, pelo `node` e sem o atalho `npx`, porque no Windows o atalho do npm deixa o
processo real rodando depois de um timeout (juntado em 2026-10-14):

```text
node node_modules/@remotion/cli/remotion-cli.js render src/index.ts ExplainerV1 <saída.mp4>
    --props=<timeline.json> --public-dir=<pasta> --gl=angle
```

Antes de chamar, o A5 confere que `remotion` e `@remotion/cli` estão em 4.0.531 no
`render/remotion/package-lock.json`. Para testar a mão dentro de `render/remotion`,
`npx remotion render ...` com os mesmos argumentos é equivalente.

- `<timeline.json>` é um `TimelineV2` válido, sem nenhum campo a mais
  (`docs/reference/schemas/timeline-v2.schema.json`).
- `<pasta>` contém `narration.wav` e cada imagem com o nome exato de `clips[].image`.
- A saída é MP4 H.264 + AAC nas dimensões do formato e na duração do áudio.
- A faixa C testa com `docs/reference/schemas/timeline-v2.example.json` e mídia sintética;
  a faixa D testa com um renderizador falso que só confere a chamada.

### Fronteira da faixa E com as faixas C e D

A faixa E entrega ao A5 um `SceneClipsManifest` válido (`src/pipeline/scene_clips.py`) e os
MP4 que ele lista; a faixa D lê esse manifesto, confere `cuts_sha256` contra os próprios
cortes (`scene_spans`) e monta um `TimelineV3`; a faixa C faz o `ExplainerV1` desenhar o
vídeo de um clipe do v3 em vez da imagem, testando com
`docs/reference/schemas/timeline-v3.example.json`. A geração real (ComfyUI e LTX-2.3) usa a
GPU inteira e cerca de 60 GB de RAM: só com combinação prévia e com nenhuma outra faixa
gerando ao mesmo tempo. Testes de faixa usam um ComfyUI falso.

## Etapas seguintes (ainda sem faixa)

5. Canais: tabela, migration, páginas do painel e vínculo da run.
6. Sugestão de nicho.
7. Validação do formato long (limites de tempo, quantidade de imagens, narração longa).
