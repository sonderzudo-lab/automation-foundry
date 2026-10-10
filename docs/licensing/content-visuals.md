# Licenças e gate de proveniência dos visuais

Status: `CONTENT_VISUAL_BACKEND=disabled` por padrão. O runtime oferece somente
o modo opt-in `local_assets_quality_test`, que importa PNGs já selecionados pelo
operador; nenhum modelo local ou provedor de stock é chamado pela aplicação.

Esta é uma decisão técnica de produto, não aconselhamento jurídico. A revisão
abaixo registra candidatos e obrigações conhecidas; não equivale à aprovação de
um backend.

## Contrato implementado

- A3 consome somente o bundle A1 aprovado e o WAV A2 conferidos por tamanho e
  SHA-256.
- A quantidade é proporcional ao áudio, com um visual a cada seis segundos por
  padrão (o backend de importação local aceita uma faixa, descrita abaixo) e
  limite de 60.
- Cada adapter precisa declarar provider, modelo, ID/URI da fonte, licença,
  atribuição e `commercial_use=True` para cada arquivo.
- O pipeline aceita apenas PNG completo, com CRC, dimensões, dados e fim do
  arquivo validados; short usa 1080x1920 e long usa 1920x1080.
- Imagens e manifesto são publicados por rename atômico e registrados como
  artifacts internos. O WAV não é reescrito.
- A run registra contagem, cobertura temporal, energia estimada para fila GPU e
  custo externo observado. Nenhum upload ou publicação é executado.

## Importação local opt-in

`local_assets_quality_test` é uma ponte de avaliação para usar imagens próprias
ou licenciadas sem introduzir um provedor externo. Ela roda na fila `io`, não
registra energia de GPU e continua sujeita à revisão humana do pacote visual.
Uma declaração no manifesto é evidência operacional do operador, não prova
jurídica independente nem autorização automática para monetização.

Configuração:

```dotenv
CONTENT_VISUAL_BACKEND=local_assets_quality_test
CONTENT_VISUAL_IMPORT_ROOT=./imports/content-visuals
CONTENT_VISUAL_MANIFEST_PATH=manifest.json
```

O caminho do manifesto é resolvido dentro da raiz de importação. A quantidade
de imagens é a que o manifesto declarar, desde que cada imagem fique de 4 a 10
segundos na tela: no mínimo um PNG a cada dez segundos de áudio (arredondado
para cima) e no máximo um a cada quatro (arredondado para baixo), entre 1 e 60.
Uma narração de 45 s aceita de 5 a 11 imagens. Fora dessa faixa a run falha com
`CONTENT_VISUALS_COUNT_MISMATCH`. Não há repetição automática. Short exige
1080x1920; long exige 1920x1080. Arquivos, IDs de fonte e nomes devem ser únicos.
Depois que A3 publica o bundle, a quantidade usada fica fixa para aquela run,
mesmo que o manifesto mude.

Exemplo de `imports/content-visuals/manifest.json` para uma imagem própria:

```json
{
  "schema_version": 1,
  "assets": [
    {
      "relative_path": "imagem-001.png",
      "sha256": "<64 caracteres hexadecimais>",
      "source_type": "owned",
      "provider": "local-owner",
      "model_id": "not-applicable",
      "source_id": "acervo-2026-001",
      "source_uri": "local-asset://operator/acervo-2026-001",
      "license_id": "OWNER-ATTESTED",
      "commercial_use": true,
      "attribution": "Acervo próprio do operador"
    }
  ]
}
```

Para stock, use `source_type: "stock"`, a página original em HTTPS como
`source_uri`, a licença/termo aplicável e a atribuição exigida. Para imagem
gerada fora da Foundry, use `generated`, identifique engine/modelo e mantenha a
evidência de direitos. A execução recusa manifesto ou asset fora da raiz,
campos extras/ausentes, `commercial_use` diferente de `true`, duplicatas, hash
divergente, PNG inválido ou dimensões incorretas. Os arquivos de importação não
são alterados; A3 copia os bytes conferidos para seu diretório de artifacts.

A faixa exata só é conhecida depois que a narração existe. Com o gate A2
habilitado, a página de revisão da narração informa a faixa aceita e confere
o manifesto naquele momento: quantidade dentro da faixa, campos, uso comercial,
existência dos arquivos e SHA-256, sem copiar nada. Enquanto o import seria
recusado, a aprovação pelo painel não é registrada e a revisão continua aberta
para o operador terminar o manifesto. Dimensões e validade do PNG continuam
sendo conferidas apenas por A3.

## Candidatos ainda bloqueados

| Candidato | Evidência primária | Decisão atual |
| --- | --- | --- |
| FLUX.1 Schnell | O model card oficial declara Apache-2.0 e uso pessoal, científico e comercial, mas o repositório gated não contém `LICENSE`/`NOTICE`; CLIP-L não declara licença no upstream e seu card exclui qualquer uso implantado do escopo. | **Não aprovado para conteúdo monetizado.** O dataset de treino não é divulgado, não há regra de titularidade do output, safety checker, watermark ou C2PA, e a carga oficial exige aproximadamente 50 GB de RAM/VRAM com offload obrigatório na RTX 3090. |
| SDXL base 1.0 | O checkpoint oficial usa CreativeML Open RAIL++-M, não Apache-2.0, e possui restrições de uso. A licença afirma também que os dados não são licenciados por ela. | Não aprovado. Exige análise separada da licença, restrições, atribuições, componentes e adequação editorial; a documentação histórica que o chamava de Apache estava incorreta. |
| Pexels API | Os termos da API remetem à licença e aos Termos de Serviço, pedem link proeminente ao Pexels e crédito ao fotógrafo quando possível, aplicam rate limits e proíbem distribuição standalone, cópia sistemática e serviço concorrente. | Não aprovado. Um adapter deve baixar o arquivo, persistir página/fotógrafo/atribuição, limitar consultas e demonstrar que o vídeo é uma obra editorial, não redistribuição standalone. |
| Pixabay API | A documentação exige cache de requests por 24 horas, proíbe downloads sistemáticos e hotlink permanente e retorna 429 ao exceder o limite. | Não aprovado. Um adapter deve baixar para storage, guardar page URL/autor/licença, respeitar cache/rate limit e passar por revisão completa dos Termos e da licença do conteúdo. |

Fontes primárias consultadas:

- FLUX.1 Schnell: <https://huggingface.co/black-forest-labs/FLUX.1-schnell>;
- SDXL base 1.0 e licença: <https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0>;
- Pexels API e termos: <https://www.pexels.com/api/documentation/> e
  <https://help.pexels.com/hc/en-us/articles/900005880463-What-are-the-Terms-and-Conditions>;
- Pixabay API: <https://pixabay.com/api/docs/>.

## Auditoria do FLUX.1 Schnell

Decisão de 2026-08-11: não habilitar. `CONTENT_VISUAL_BACKEND` continua aceitando
somente `disabled`. Esta decisão não afirma que o modelo seja ilegal; ela afirma
que a evidência disponível não satisfaz o gate comercial fail-closed deste
projeto.

### Identidade e acesso

- repositório: `black-forest-labs/FLUX.1-schnell`;
- revisão observada: `741f7c3ce8b383c54771c7003378a50191e9efe9`;
- acesso: repositório Hugging Face `gated: auto`, com conta, aceite das
  condições e token de leitura;
- licença declarada: tag `apache-2.0` e texto do model card, sem arquivo
  `LICENSE` ou `NOTICE` no repositório dos pesos;
- tamanho do layout Diffusers: aproximadamente 33,72 GB; o repositório completo
  inclui cerca de 24,1 GB de bundles single-file duplicados e não deve ser
  baixado integralmente.

Hashes LFS observados para o layout mínimo:

| Componente | SHA-256 |
| --- | --- |
| transformer shard 1/3 | `9b633dbe87316385c5b1c262bd4b5a01e3d955170661d63dcec8a01e89c0d820` |
| transformer shard 2/3 | `58b4434078f0c2567ddc54e3b5cbf39626ab55fbd9d5c22956e183668f535dec` |
| transformer shard 3/3 | `e2cbc25471ed5186e69a9b51098300cb2f612556453e38a372c851a220ed238d` |
| CLIP-L | `893d67a23f4693ed42cdab4cbad7fe3e727cf59609c40da28a46b5470f9ed082` |
| T5 shard 1/2 | `ec87bffd1923e8b2774a6d240c922a41f6143081d52cf83b8fe39e9d838c893e` |
| T5 shard 2/2 | `a5640855b301fcdbceddfa90ae8066cd9414aff020552a201a255ecf2059da00` |
| VAE | `f5b59a26851551b67ae1fe58d32e76486e1e812def4696a4bea97f16604d40a3` |
| tokenizer T5 `spiece.model` | `d60acb128cf7b7f2536e8f38a5b18a05535c9e14c7a355904270e15b0945ea86` |

Esses hashes não fecham o inventário. Arquivos JSON, índices de shards e
tokenizers não-LFS também controlam o que é carregado e precisam ser baixados,
hasheados localmente e congelados antes de qualquer adapter opt-in. Fixar apenas
os pesos grandes permite troca silenciosa de configuração.

### Riscos que impedem aprovação comercial

1. O repositório gated não apresenta o texto da licença nem avisos por
   componente; a concessão Apache-2.0 é inferida do metadado e do card.
2. O `text_encoder` e tokenizer CLIP-L vêm de
   `openai/clip-vit-large-patch14`, cujo repositório de pesos não declara
   licença; o próprio card informa que qualquer uso implantado, comercial ou
   não, está fora do escopo. A licença MIT do código CLIP não licencia esses
   pesos.
3. O upstream não divulga datasets, proveniência, consentimento ou opt-out de
   treinamento. O projeto não pode verificar pessoas, marcas ou direitos
   incorporados aos dados.
4. O model card não define titularidade ou cessão dos outputs. A licença dos
   pesos não resolve direitos sobre a imagem produzida.
5. O pipeline não fornece safety checker, watermark ou C2PA. Moderação,
   disclosure de conteúdo sintético e revisão de pessoas/marcas seriam
   responsabilidade integral do chamador e do operador.
6. O card não apresenta o modelo como fonte factual. Imagens geradas não podem
   representar evidência, dado, gráfico, documento, mapa ou captura de tela.

O item de datasets/componentes do gate abaixo não é apenas um trabalho ainda
não executado: com o upstream atual ele é inatingível. Uma futura exceção
exigiria decisão explícita do proprietário aceitando esse risco residual; um
teste técnico na GPU não concede aprovação comercial.

### Limites para um possível quality test

Um futuro adapter poderia chamar-se somente `flux_schnell_quality_test`, nunca
`flux`, `flux_schnell` ou outro nome que pareça aprovado. Antes de entrar em
`Settings`, ele ainda precisaria:

- operar somente com cache local, revisão exata e inventário SHA-256 completo;
- usar `diffusers>=0.30,<0.40`, `local_files_only=True` e recusar download
  implícito durante uma run;
- exigir CUDA, BF16, offload para CPU, RAM e disco suficientes; a documentação
  oficial estima aproximadamente 50 GB de RAM/VRAM para todos os componentes;
- travar `guidance_scale=0.0`, de 1 a 4 steps,
  `max_sequence_length<=256`, dtype BF16 e seed persistida por asset;
- declarar `commercial_use=False` e persistir os riscos de CLIP-L, datasets,
  safety checker, watermark e C2PA;
- recusar prompts com pessoas reais, cargos públicos, marcas, "no estilo de" e
  pedidos de texto, dados, gráficos ou documentos;
- exigir revisão humana de semelhança com pessoa real, marcas, artefatos e
  adequação editorial antes de montagem.

## Candidatos auditados em 2026-10-10

Auditoria somente documental, feita nas páginas e na API pública do Hugging
Face. Nada foi baixado, instalado nem executado, e `CONTENT_VISUAL_BACKEND`
continua aceitando apenas `disabled` e `local_assets_quality_test`. O objetivo
foi reavaliar, para três modelos mais novos, os riscos que reprovaram o FLUX.1
Schnell.

| Item | FLUX.2 klein 4B | Z-Image Turbo | Qwen-Image |
| --- | --- | --- | --- |
| Repositório | `black-forest-labs/FLUX.2-klein-4B` | `Tongyi-MAI/Z-Image-Turbo` | `Qwen/Qwen-Image` |
| Revisão observada | `e7b7dc27f91deacad38e78976d1f2b499d76a294` | `f332072aa78be7aecdf3ee76d5c247082da564a6` | `75e0b4be04f60ec59a75f475837eced720f823b6` |
| Acesso | público, sem gate | público, sem gate | público, sem gate |
| Licença declarada | Apache-2.0 no card ("commercial use") | Apache-2.0 só no metadado | Apache-2.0 no card |
| Arquivo de licença junto dos pesos | `LICENSE.md` com o texto Apache-2.0, sem aviso de copyright nem apêndice | **não há** | `LICENSE` existe; conteúdo não lido nesta auditoria |
| Text encoder | arquitetura `Qwen3ForCausalLM` (2560 × 36 camadas); o repositório não nomeia o modelo de origem | arquitetura `Qwen3ForCausalLM` (2560 × 36 camadas); origem não nomeada | arquitetura `Qwen2_5_VLForConditionalGeneration` (3584 × 28 camadas); origem não nomeada |
| Dados de treino | só afirma que filtrou NSFW e CSAM conhecido (parceria com a IWF); **não lista datasets, consentimento ou opt-out** | **não mencionados** | **não mencionados** |
| Titularidade do output | não tratada | não tratada | não tratada |
| Segurança | filtros de entrada/saída e exemplo de watermark no código de inferência de referência; C2PA só na API paga; lista de usos proibidos no card | nada descrito | nada descrito |
| Requisito declarado | cerca de 13 GB de VRAM, cita RTX 3090 | "16G VRAM", 8 passos, guidance 0 | não declarado; exemplo usa 50 passos |
| Layout Diffusers | cerca de 16,0 GB (sem o bundle single-file duplicado de 7,75 GB) | cerca de 32,8 GB | não medido; transformer em 9 shards |

### O que mudou em relação ao FLUX.1 Schnell

- **Risco 1 (licença só no metadado):** resolvido no FLUX.2 klein 4B e,
  aparentemente, no Qwen-Image, que trazem o arquivo de licença ao lado dos
  pesos. Continua aberto no Z-Image Turbo.
- **Risco 2 (CLIP-L sem licença):** nenhum dos três usa CLIP-L. Os text
  encoders têm a arquitetura Qwen3 ou Qwen2.5-VL; o upstream `Qwen/Qwen3-4B`
  (revisão `1cfa9a7208912126459214e8b04321603b3df60c`) é Apache-2.0 com arquivo
  `LICENSE`. Nenhum repositório, porém, declara de qual checkpoint os pesos do
  encoder foram copiados: a correspondência é inferida das dimensões e não foi
  confirmada por hash.
- **Risco 3 (dados de treino não divulgados):** **continua aberto nos três.** O
  FLUX.2 klein 4B é o único que diz algo, e só sobre filtragem; nenhum informa
  datasets, proveniência, consentimento ou opt-out.
- **Risco 4 (titularidade do output):** continua aberto nos três.
- **Risco 5 (safety checker, watermark, C2PA):** melhor no FLUX.2 klein 4B, mas
  os filtros e o watermark ficam no código de inferência de referência; não foi
  verificado se o pipeline `diffusers` os aplica. Nada nos outros dois.
- **Risco 6 (não é fonte factual):** o card do FLUX.2 klein 4B repete o aviso;
  a regra de não gerar dado, gráfico, documento ou captura de tela permanece.

Observações adicionais:

- O card do FLUX.2 klein 4B lista usos proibidos (conteúdo ilegal, enganoso,
  íntimo não consensual, decisão automatizada de alto risco) que não fazem parte
  do texto Apache-2.0. O efeito jurídico dessa lista não foi avaliado.
- A variante FLUX.2 klein **9B** é não comercial e não é candidata.
- Hashes LFS observados para o FLUX.2 klein 4B: transformer
  `9f29f9edcfdae452a653ffb51a534ca4decd389952c225724ff3b94042612a6e`; text
  encoder `8c0506e7f4936fa7e26183a4fd8da4e2bdbc5990ba64ae441f965d51228f36ea` e
  `82f2bd839378541b0557bfabaf37c7d3d637071fdcb73302dedd7cf61162ce07`; VAE
  `ca70d2202afe6415bdbcb8793ba8cd99fd159cfe6192381504d6c4d3036e0f04`. Como no
  FLUX.1, eles não fecham o inventário: JSONs e tokenizers também precisam ser
  congelados.
- Qualidade em português, aderência ao prompt, tempo e VRAM reais na RTX 3090
  não foram medidos.

### Decisão

Nenhum dos três é aprovado para conteúdo monetizado. O motivo decisivo é o
risco 3: o proprietário decidiu, em 2026-10-09 (D4), não aceitar por enquanto
modelos de imagem com dataset de treino não divulgado, e os três estão nessa
situação. Visuais monetizados continuam com acervo próprio, stock ou geração
externa com direitos declarados no manifesto.

Se a D4 for revista, o FLUX.2 klein 4B é o candidato com a melhor evidência:
licença em arquivo, sem gate, sem CLIP-L, mitigações descritas e requisito
compatível com a RTX 3090. Mesmo assim ele só poderia entrar como
`flux2_klein_quality_test`, com os limites descritos para o FLUX.1 acima, e
qualquer teste de geração precisa de autorização para o download e deve
acontecer fora do pipeline, porque A3 exige `commercial_use=True` por arquivo.

Fontes primárias consultadas em 2026-10-10:

- <https://huggingface.co/black-forest-labs/FLUX.2-klein-4B> (card,
  `LICENSE.md`, `text_encoder/config.json` e listagem de arquivos);
- <https://huggingface.co/Tongyi-MAI/Z-Image-Turbo> (card,
  `text_encoder/config.json` e listagem de arquivos);
- <https://huggingface.co/Qwen/Qwen-Image> (card, `text_encoder/config.json` e
  listagem de arquivos);
- <https://huggingface.co/Qwen/Qwen3-4B> (metadados e presença de `LICENSE`).

## Gate para habilitar um backend

Antes de alterar `Settings`, a opção escolhida precisa ter:

1. nome, versão/revisão e hashes fixados;
2. licença de engine, pesos, datasets/componentes e output documentada;
3. regras de atribuição e source URI representadas no manifesto A3;
4. limites de conteúdo, privacidade, pessoas/marcas e uso editorial explícitos;
5. teste opt-in no hardware ou API real, incluindo retry, rate limit, VRAM,
   duração, qualidade e ausência de hotlink;
6. revisão humana do pacote visual antes de qualquer montagem/publicação.

Para FLUX.1 Schnell, os itens 2 e 4 não estão cumpridos. A revisão/hashes acima
documentam o alvo auditado, mas não autorizam adicionar o backend ao runtime.
