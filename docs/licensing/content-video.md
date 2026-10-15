# Licenças das cenas animadas por IA

Status: nenhum backend implementado. O plano está em
`docs/planning/scene-animation-ltx.md`; o teste fora da pipeline está em
`D:\automation-foundry\labs\video-wan22-i2v\`.

Esta é uma decisão técnica de produto, não aconselhamento jurídico.

## Aceite do proprietário

Em 2026-10-15, perguntado no chat da sessão principal, o proprietário aceitou a
LTX-2 Community License Agreement para usar o LTX-2.3 em conteúdo do canal.

- Licença: LTX-2 Community License Agreement, de 5 de janeiro de 2026, que vale para as
  versões LTX-2 publicadas desde então, inclusive o LTX-2.3, até 11 de agosto de 2026
  (`https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2`; texto também em
  `Lightricks/LTX-2.3@3c6a4e66e5d0a684231950b9c74dd4ded7b6fadc/LICENSE`).
- Uso gratuito para entidades com receita anual abaixo de US$ 10.000.000. Acima disso a
  licença exige um acordo comercial pago com a Lightricks, e o uso precisa parar até
  existir esse acordo.
- A licença traz uma lista de usos proibidos, que vale para o conteúdo gerado.
- Versões publicadas a partir de 11 de agosto de 2026, como o LTX-2.5, seguem outro
  texto (LTX-2.x Community License Agreement) e não estão cobertas por este aceite.

## Escopo dos clipes

O aceite cobre o modelo de vídeo, não a imagem de origem. Cada clipe herda o escopo da
imagem aprovada no A3: enquanto as imagens geradas pelo FLUX.2 klein forem
`quality-test-only` (decisão sobre dados de treino não divulgados, em
`content-visuals.md`), os clipes também são, e o rótulo segue até o export local.

## Pesos e ferramentas usados no teste

| Item | Origem | Licença |
| --- | --- | --- |
| `ltx-2.3-22b-distilled-fp8.safetensors` | `Lightricks/LTX-2.3-fp8@1d756cd27fa11c0896c4dfee093cd1bf36c7f7a1` | LTX-2 Community License |
| `ltx-2.3-spatial-upscaler-x2-1.1.safetensors` | `Lightricks/LTX-2.3@3c6a4e66e5d0a684231950b9c74dd4ded7b6fadc` | LTX-2 Community License |
| `gemma_3_12B_it_fp4_mixed.safetensors` | `Comfy-Org/ltx-2@ccde4ba417d7900669fd56dd292a883cee11ff37` | declarado no repositório como LTX-2 Community License; os termos do Gemma não foram lidos na fonte |
| ComfyUI v0.40.0 | `comfyanonymous/ComfyUI@3541246a` | GPL-3.0, usado como ferramenta local, sem redistribuição |
| SageAttention 2.2.0 post6 (Windows) | `woct0rdho/SageAttention` | Apache-2.0 |
| triton-windows 3.7.1.post27 | PyPI | MIT |

SHA-256 dos arquivos de modelo: `D:\automation-foundry\labs\video-wan22-i2v\models-ltx.json`.

Pendente: ler na fonte os termos do Gemma 3 que acompanham o text encoder antes do
primeiro vídeo monetizado.
