# Licenças e gate de proveniência das legendas

Status: `CONTENT_CAPTION_BACKEND=disabled` por padrão. O modo opt-in
`approved_text_timing_quality_test` gera timings estimados sem engine ou pesos
externos; nenhum modelo de transcrição está selecionável no runtime.

Esta é uma decisão técnica de produto, não aconselhamento jurídico. A licença
de um pacote Python não concede automaticamente direitos sobre pesos, datasets
ou outputs de um modelo associado.

## Contrato implementado

- A4 consome somente o WAV A2 conferido por tamanho e SHA-256 e a narração
  vinculada ao digest da approval A1.
- O adapter precisa declarar backend, provider, modelo, idioma, licença e
  `commercial_use=True`.
- Cada palavra precisa ter texto limitado, intervalo finito, positivo,
  monotônico e dentro da duração do áudio.
- O transcript normalizado precisa manter similaridade mínima de 0,85 com a
  narração aprovada, reduzindo risco de alucinação silenciosa.
- O arquivo ASS usa timestamps absolutos e tags karaoke por palavra, é publicado
  por rename atômico e registrado como artifact interno.
- A run registra contagem de palavras, similaridade, energia estimada da fila
  GPU e custo externo observado. Nenhum vídeo ou upload é produzido.

## Timing determinístico opt-in

`approved_text_timing_quality_test` usa exatamente a narração aprovada em A1 e
a duração conferida do WAV A2. As palavras recebem intervalos proporcionais ao
tamanho dos tokens, cobrindo o áudio do instante zero ao fim. O step roda em
`cpu`, declara `deterministic-proportional-v1`/`PROJECT-INTERNAL`, não baixa
nada, não chama API e não registra energia de GPU.

```dotenv
CONTENT_CAPTION_BACKEND=approved_text_timing_quality_test
```

Esse modo serve para validar o encadeamento e produzir uma primeira legenda
queimada local. Ele **não escuta o áudio**, não detecta pausas, pronúncia,
omissões ou deriva do TTS e não deve ser apresentado como word alignment real.
Uma revisão humana do vídeo continua obrigatória antes de monetização ou
publicação. O validador A4 ainda exige o texto exato, timings monotônicos e
confinados à duração e todos os artifacts permanecem vinculados por hash.

## Modelo de alinhamento ainda bloqueado

`faster-whisper` continua apenas candidato para substituir a estimativa por
timestamps observados. Antes de criar esse backend, a revisão deve separar e
fixar:

1. versão e licença do engine, runtime CTranslate2 e demais dependências;
2. identidade, revisão e hashes exatos dos pesos escolhidos;
3. licença e proveniência documentada dos pesos e datasets de treinamento;
4. direito comercial e obrigações de atribuição aplicáveis;
5. idioma PT-BR, timestamps por palavra, taxa de omissão/alucinação e qualidade
   de pontuação em amostras reais;
6. VRAM, duração, timeout e recuperação na RTX 3090;
7. execução somente com cache local e sem download implícito durante uma run.

Até esse inventário e o teste opt-in existirem, nomes como `faster_whisper`,
`whisper` ou variantes permanecem recusados por configuração e pelo adapter
fail-closed.
