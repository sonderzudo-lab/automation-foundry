# ExplainerV1 — faixa C

Render local de `TimelineV2`, recebido diretamente em `--props`. A entrada canônica
é `src/pipeline/timeline.py` e o exemplo fica em
`docs/reference/schemas/timeline-v2.example.json`, na raiz do repositório.
Não há props adicionais, chamadas de rede, fontes baixadas nem dependência do painel.

## Preparação

Node.js >= 22.18 é necessário para os testes que importam TypeScript nativamente.
Remotion e CLI estão fixados em **4.0.531**, React e React DOM em **19.2.0**.
TypeScript **5.9.3** e tipos React/React DOM **19.2.0** são ferramentas de desenvolvimento.

Somente depois de autorização do proprietário, execute `npm ci` nesta pasta.
O navegador precisa existir em `node_modules/.remotion/chrome-headless-shell/`.
Pode ser copiado da instalação local do spike. Qualquer download do navegador exige
autorização separada. Não execute o render com o navegador ausente: a CLI pode tentar baixá-lo.
`node_modules/` e `out/` são ignorados pelo Git.

## Contrato com a faixa D

Com cwd nesta pasta, a chamada permanece:

```text
node node_modules/@remotion/cli/remotion-cli.js render <absoluto>/render/remotion/src/index.ts ExplainerV1 <saída.mp4> --props=<timeline.json> --public-dir=<pasta> --gl=angle
```

A pasta pública contém `narration.wav` e as imagens com os nomes exatos de
`clips[].image`. Todos são carregados por `staticFile()`.
`calculateMetadata` valida os props e define dimensões e
`ceil(duration_ms * 30 / 1000)` frames. A configuração fixa H.264, AAC, yuv420p e
concorrência 1. Duração não múltipla de frame é arredondada para cima, conforme o contrato.
O A5 continua responsável pela integridade dos arquivos, hashes, timeouts e publicação
atômica do MP4. A validação JavaScript não substitui esses controles.

## Animações

- Cada clip usa sua própria duração para zoom e pan. `none` mantém a imagem parada.
  O crossfade cobre `transition_ms / 2` de cada lado do corte; a imagem anterior
  permanece opaca enquanto a seguinte entra, evitando escurecimento artificial.
- `word_pop` mostra a palavra durante seu intervalo. `phrase_blocks` agrupa no máximo
  seis palavras, com quebras por pontuação, tamanho ou pausa; destaca as já faladas.
  O contrato não traz limites de eventos ASS: os blocos são derivados de `words`.
  Silêncios não prolongam a palavra anterior.
- A lista usa um cartão por item e conclui a entrada em `item_end_ms`. O número sai
  após a conclusão do item, acompanha o rótulo no canto e volta marcado quando abre
  o próximo cartão. Cartões sem intervalo fazem uma passagem direta.
- `highlight` desenha o texto do overlay. A frase final começa em `word_from`, revela
  palavras nos timestamps e aplica destaque do índice absoluto `highlight_from` em diante.
  Durante a frase final, a legenda é substituída pela própria frase animada.
- O visual usa as cores, fontes de sistema e véu escuro da demo aprovada, sem desfoque
  de fundo. A barra superior avança até o último frame. O layout considera os dois formatos.

## Verificações locais

```powershell
node --test tests/*.test.mjs
npx --no-install tsc --noEmit
node scripts/prepare-smoke.mjs
```

O preparador cria apenas mídia sintética ignorada pelo Git em `out/smoke/`:
WAV PCM mono mudo de 12 s, dois PNGs 1080×1920 com grade para inspecionar movimento,
e uma cópia exata do exemplo. Os hashes fictícios do exemplo são preservados:
este smoke verifica o renderer, não a cadeia de evidências do A5.

**Somente com GPU liberada pelo proprietário e uma faixa por vez**, na mesma pasta:

```powershell
$entry = (Resolve-Path src/index.ts).Path
$props = (Resolve-Path out/smoke/timeline.json).Path
$media = (Resolve-Path out/smoke/public).Path
node node_modules/@remotion/cli/remotion-cli.js render $entry ExplainerV1 out/smoke/explainer.mp4 "--props=$props" "--public-dir=$media" --gl=angle
ffprobe -v error -show_entries stream=codec_type,codec_name,width,height,r_frame_rate,duration -show_entries format=duration -of json out/smoke/explainer.mp4
```

Esperado: H.264 + AAC, 1080×1920, 30 fps, 12 s. Inspecione também os cartões,
a passagem entre imagens em 6 s, o número no canto e a frase final.
O teste opt-in `tests/integration/test_remotion_assembly.py` da faixa D permanece
para o proprietário após a integração. A revisão cruzada por `/code-review`
prevista em `docs/planning/lanes.md` é uma etapa externa a esta faixa.

## Evidência desta implementação

- `npx --no-install tsc --noEmit`: passou com TypeScript 5.9.3.
- `node --test tests/*.test.mjs`: 23 testes passaram, incluindo crossfade centrado,
  movimentos locais, silêncio, retorno do número, índices absolutos da frase final,
  dimensões long/short e recusa de props inválidos.
- Bundle do Remotion: compilado sem iniciar navegador.
- Lock e pacotes instalados: quatro dependências de runtime nas versões exigidas;
  todas as versões transitivas preexistentes do lock do spike foram preservadas.
- Navegador: copiado do spike para `node_modules/.remotion/`, sem download.
- Mídia sintética: pronta; ffprobe confirmou WAV PCM S16LE, mono, 48 kHz e 12 s.
- **Pendente:** render real e inspeção visual. O proprietário autorizou o teste, mas
  a GPU estava em uso por outra execução e pediu para aguardar seu aviso de liberação.
- **Pendente após integração:** teste opt-in da faixa D e revisão cruzada do diff.
