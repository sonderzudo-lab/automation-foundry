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
| D — plano de animação, timeline v2 e backend A5 (etapa 4) | Claude Code | `feat/animated-assembly` | `src/pipeline/animation_generation.py` e `src/pipeline/timeline_builder.py` (novos), `src/pipeline/assembly.py` (só o adapter novo), `src/pipeline/a1_executor.py` (só a escolha de fila e adapter do A5), `src/core/config.py` (só campos novos), `.env.example`, testes correspondentes |
| Integradora | proprietário + sessão principal | `chore/project-skills` | contratos, docs, merges |

### Fronteira entre as faixas C e D

As duas faixas só se encontram nesta chamada, que nenhuma delas pode mudar sozinha:

```text
npx remotion render render/remotion/src/index.ts ExplainerV1 <saída.mp4>
    --props=<timeline.json> --public-dir=<pasta> --gl=angle
```

- `<timeline.json>` é um `TimelineV2` válido, sem nenhum campo a mais
  (`docs/reference/schemas/timeline-v2.schema.json`).
- `<pasta>` contém `narration.wav` e cada imagem com o nome exato de `clips[].image`.
- A saída é MP4 H.264 + AAC nas dimensões do formato e na duração do áudio.
- A faixa C testa com `docs/reference/schemas/timeline-v2.example.json` e mídia sintética;
  a faixa D testa com um renderizador falso que só confere a chamada.

## Etapas seguintes (ainda sem faixa)

5. Canais: tabela, migration, páginas do painel e vínculo da run.
6. Sugestão de nicho.
7. Validação do formato long (limites de tempo, quantidade de imagens, narração longa).
