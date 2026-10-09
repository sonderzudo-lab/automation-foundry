# Fixtures sintéticas do ensaio local A1→A6

Este documento descreve exatamente o que o ensaio local do Content Engine usa
como mídia e o que essa mídia **não** prova. Ele é registro técnico de produto,
não aconselhamento jurídico.

## O que são as fixtures

`tests/support/local_media_rehearsal.py` gera toda a mídia do ensaio no momento
do teste, a partir de código deste repositório:

| Fixture | Como é produzida | O que é |
| --- | --- | --- |
| WAV de narração | tom senoidal determinístico, PCM S16LE mono 24 kHz, 12 s | **não é voz**, não é Kokoro e não é gravação humana |
| PNGs do pacote visual | quadros de cor sólida gerados por `zlib`/`struct` | **não é foto, stock nem saída de modelo generativo** |
| Roteiro A1 | texto PT-BR fixo escrito no próprio repositório | fixture editorial, não saída real do Ollama |

Nenhum download, chamada de rede, API externa, modelo ou asset de terceiro
participa da geração. O ensaio não publica, não envia mensagem e não gasta.

## Proveniência declarada no manifesto A3

O gate A3 exige `commercial_use=True` por asset. As fixtures declaram:

```json
{
  "source_type": "generated",
  "provider": "automation-foundry",
  "model_id": "synthetic-rehearsal-frame-v1",
  "source_uri": "local-model://automation-foundry/synthetic-rehearsal-frame-v1/001",
  "license_id": "PROJECT-SYNTHETIC-FIXTURE; scope=quality-test-only",
  "commercial_use": true,
  "attribution": "Quadro sintético gerado pelo próprio projeto para quality test; não é imagem de terceiro nem substitui um pacote visual revisado."
}
```

`commercial_use=True` é verdadeiro no sentido estrito de que o arquivo é obra do
próprio projeto e não incorpora direito de terceiro. O marcador
`scope=quality-test-only` no `license_id` registra que a fixture existe para
exercitar o contrato, seguindo o mesmo padrão já usado pela voz Kokoro. Ela não
autoriza publicação nem substitui a revisão humana de um pacote visual real.

O WAV usa o mesmo `license_id` e declara backend
`synthetic_quality_test_fixture`, de modo que qualquer artifact produzido a
partir dele fica identificável no banco como ensaio, não como narração real.

## O que o ensaio prova

- A3 importa somente arquivos escolhidos pelo operador dentro da raiz contida,
  na quantidade exata calculada pelo áudio, com SHA-256 por item, e não altera
  a origem.
- A4 distribui a narração aprovada pela duração real do WAV e publica ASS com
  karaoke coerente com o áudio conferido.
- A5 reconcilia áudio, manifesto, imagens e legenda e monta um MP4 real.
- A6 avalia originalidade e grava relatório antes de qualquer upload.
- A cadeia inteira permanece observável, idempotente e fail-closed.

## O que o ensaio não prova

- qualidade, naturalidade ou licenciamento da narração PT-BR real;
- direitos sobre imagens reais, sejam próprias, stock ou geradas por modelo;
- sincronismo de legenda contra uma voz real, incluindo pausas e deriva;
- formato long, carga prolongada, VRAM e desempenho na RTX 3090;
- adequação editorial do conteúdo.

Essas verificações continuam dependentes de Kokoro na GPU local, de um pacote
visual revisado e de revisão humana. Consulte também
[`content-tts.md`](content-tts.md), [`content-visuals.md`](content-visuals.md),
[`content-captions.md`](content-captions.md) e
[`content-assembly.md`](content-assembly.md).
