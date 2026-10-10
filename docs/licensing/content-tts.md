# Licenças e gate de qualidade do TTS

Status: Kokoro pode ser executado somente como `kokoro_quality_test`. O backend
continua `disabled` por padrão e nenhuma voz PT-BR está aprovada para conteúdo
monetizado.

Esta é uma decisão técnica de produto, não aconselhamento jurídico.

## Inventário fixado

| Camada | Identidade fixada | Licença/evidência | Decisão atual |
| --- | --- | --- | --- |
| Engine Python | `kokoro==0.9.4` | Apache-2.0 no pacote e no repositório | Permitido apenas no modo de teste local enquanto a voz não for aprovada. O pacote publicado requer Python `>=3.10,<3.13`; o projeto fixa Python 3.12. |
| G2P Python | `misaki>=0.9.4`, dependência do pacote | Apache-2.0 | Permitido. PT-BR não usa um G2P próprio: cai no backend eSpeak NG `pt-br`. |
| Modelo | `hexgrad/Kokoro-82M` na revisão `f3ff3571791e39611d31c381e3a41a3af07b4987` | Pesos Apache-2.0; SHA-256 `496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4` | Permitido para avaliação. O adapter recusa outro digest. |
| eSpeak NG | instalação do sistema operacional | GPL-3.0-or-later | Dependência externa obrigatória para `lang_code='p'`. Não venderizar nem ligar estaticamente. Se o binário for redistribuído, cumprir a GPL e revisar o pacote de distribuição. |
| Vozes PT-BR | `pf_dora`, `pm_alex`, `pm_santa` | Publicadas no repositório Apache-2.0 dos pesos; prefixes SHA-256 `07e4ff98`, `cf0ba8c5`, `d4210316` | Somente teste de qualidade. A documentação upstream não informa nota, duração de treino ou proveniência individual dessas vozes. |

Fontes primárias:

- pacote e metadados: <https://pypi.org/project/kokoro/0.9.4/>;
- engine: <https://github.com/hexgrad/kokoro>;
- modelo e dados declarados: <https://huggingface.co/hexgrad/Kokoro-82M>;
- catálogo de vozes: <https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md>;
- licença do Misaki: <https://github.com/hexgrad/misaki/blob/main/LICENSE>;
- licença e instalação do eSpeak NG: <https://github.com/espeak-ng/espeak-ng>.

## Candidatos auditados em 2026-10-10

Monetização passou a ser meta do proprietário, e o Kokoro segue bloqueado para
isso pela proveniência das vozes PT-BR. Foram lidos, nos repositórios oficiais,
o arquivo de licença do código, o model card e o inventário de arquivos de dois
candidatos. Nada foi baixado, instalado ou executado: esta seção não aprova
backend nenhum e não substitui a audição nem a decisão do proprietário.

| | Chatterbox Multilingual PT-BR | Qwen3-TTS Base |
| --- | --- | --- |
| Repositório dos pesos | `ResembleAI/Chatterbox-Multilingual-pt-br`, revisão `b3952f18bc2eaa72b9bd7c17d2c4653bcad4770d` (base: `ResembleAI/chatterbox`, `5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18`) | `Qwen/Qwen3-TTS-12Hz-1.7B-Base`, revisão `fd4b254389122332181a7c3db7f27e918eec64e3` (0.6B: `5d83992436eae1d760afd27aff78a71d676296fc`) |
| Código | `resemble-ai/chatterbox`, commit `5de7a54aa4e5e2baadb0182dde554908b48b85c2`; arquivo `LICENSE` MIT, copyright Resemble AI | `QwenLM/Qwen3-TTS`, commit `022e286b98fbec7e1e916cb940cdf532cd9f488e`; arquivo `LICENSE` Apache-2.0 |
| Licença dos pesos | `license: mit` no model card; o repositório dos pesos não traz arquivo `LICENSE` | `license: apache-2.0` no model card; o repositório dos pesos não traz arquivo `LICENSE` |
| Acesso | Público, sem gate | Público, sem gate |
| Português | Finetune dedicado, locale `pt-BR`, language ID `pt` | "Portuguese" entre dez idiomas, sem variante brasileira declarada |
| Vozes | Sem voz pronta no pacote PT-BR: clona a partir de um áudio de referência | Clona a partir de 3 s de referência com transcrição; as nove vozes prontas ficam em outro modelo (`CustomVoice`) e nenhuma é nativa de português |
| Dados de treino | "0.5M hours of cleaned data" e "prompts are sourced from freely available data on the internet"; os dados do finetune PT-BR não são descritos | Não descritos no repositório nem no model card |
| Marca d'água | PerTh imperceptível em todo áudio gerado (biblioteca MIT) | Nenhuma mencionada |
| Pesos principais | `t3_pt_br.safetensors` 2,14 GB, SHA-256 `074aaf65255eb9cb960288f7cc72e09d3b5008f6e0b14868c0d4e5b0bd7cbb6c`; `s3gen_v3.safetensors` 1,06 GB, `4a46190f3dccc2230fbb3488a930bccc925862ee68f2662433dfcfe93ce6c2cb` | `model.safetensors` 3,86 GB, `38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c`; `speech_tokenizer/model.safetensors` 0,68 GB, `836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258` |
| Dependências fixadas pelo pacote | `torch==2.6.0`, `torchaudio==2.6.0`, `transformers==5.2.0`, `numpy<2`, `gradio==6.8.0`, e `resemble-perth` instalado do branch `master`, sem commit fixo | `transformers==4.57.3`, `accelerate==1.12.0`, `sox`, `onnxruntime`; `torch` sem versão fixa |

Fontes primárias: <https://github.com/resemble-ai/chatterbox>,
<https://huggingface.co/ResembleAI/Chatterbox-Multilingual-pt-br>,
<https://huggingface.co/ResembleAI/chatterbox>,
<https://github.com/resemble-ai/Perth>, <https://github.com/QwenLM/Qwen3-TTS> e
<https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base>.

### O que muda em relação ao Kokoro

O bloqueio do Kokoro é a identidade das vozes: `pf_dora`, `pm_alex` e
`pm_santa` são pessoas ou composições cuja origem o upstream não documenta. Os
dois candidatos não dependem de voz pronta. A voz é a do áudio de referência, e
uma referência gravada pelo próprio proprietário, ou por alguém contratado com
licença escrita para esse uso, tem proveniência conhecida. É a alternativa que
este documento já apontava como preferível.

### Riscos que continuam abertos

1. **Dados de treino não divulgados nos dois.** Nenhum upstream lista datasets,
   consentimento ou opt-out. O Chatterbox declara apenas volume e que as
   amostras vêm de dados livremente disponíveis na internet. É o mesmo tipo de
   lacuna que reprovou o FLUX.1 em `content-visuals.md` e só sai por decisão
   explícita do proprietário aceitando o risco residual.
2. **Licença dos pesos declarada só por metadado.** Nos dois casos a concessão
   está no front-matter do model card, sem arquivo `LICENSE` ao lado dos pesos.
   O código tem o arquivo; os pesos não.
3. **Titularidade do áudio gerado não é tratada** por nenhuma das duas licenças.
4. **Componentes do Chatterbox.** O model card credita CosyVoice, HiFT-GAN e
   Llama 3. O código instancia a arquitetura Llama do `transformers` com uma
   configuração própria e carrega os pesos da Resemble; não baixa pesos da Meta.
   A licença de cada componente creditado não foi lida nesta auditoria.
5. **`conds.pt` do repositório base** é uma voz padrão sem origem documentada e
   não deve ser usada.
6. **Clonagem de voz** só com referência do próprio proprietário ou com
   autorização escrita do locutor. Nenhuma voz de terceiro, pública ou não.
7. **Marca d'água do Chatterbox** identifica o áudio como sintético e não deve
   ser removida; é compatível com a divulgação de conteúdo sintético que as
   plataformas pedem, mas a regra de cada plataforma não foi auditada aqui.

### Pendências técnicas antes de qualquer adapter

- Os dois pacotes fixam versões que conflitam com o runtime atual (PyTorch
  `2.7.1+cu128` do adapter Kokoro). Um teste de audição deve rodar em ambiente
  virtual separado, fora do `.venv` do projeto.
- O `resemble-perth` precisa de commit fixo; instalar do `master` não é
  reproduzível.
- O model card do pacote PT-BR diz que ele depende do código e de arquivos do
  repositório base, e o README só documenta o carregamento do multilíngue V3
  (`language_id="pt"`). O carregamento do pacote dedicado precisa ser confirmado
  na prática.
- O pacote `sox` do Qwen3-TTS pode exigir o binário SoX externo; a licença dele
  não foi verificada.
- VRAM, velocidade e qualidade em PT-BR não foram medidas. As tabelas de WER do
  Qwen3-TTS são do próprio fornecedor.

### Recomendação técnica

Testar primeiro o Chatterbox PT-BR, por ser o único com finetune brasileiro
dedicado, e o Qwen3-TTS 1.7B Base como segundo, ambos com uma referência da voz
do proprietário e em ambiente isolado. Um adapter só entra em `Settings` depois
da audição, com nome `*_quality_test`, revisão e hashes fixados, e a mudança de
escopo para conteúdo monetizado continua dependendo da decisão explícita sobre
os riscos 1 a 3.

## Controles implementados

- `CONTENT_TTS_BACKEND=disabled` permanece o default.
- Não existe backend genérico chamado `kokoro`; esse valor é recusado.
- `kokoro_quality_test` aceita somente idioma `p` e as três vozes PT-BR
  inventariadas.
- Pacote, revisão do repositório, peso principal e prefixo do digest da voz são
  verificados antes da inferência.
- O adapter exige CUDA e eSpeak NG no `PATH`, reutiliza um singleton por worker,
  limita cada trecho a 400 caracteres e grava WAV PCM S16LE mono a 24 kHz.
- A proveniência persistida no artifact/metrics declara Apache-2.0, eSpeak NG
  externo GPL-3.0-or-later, voz sem proveniência individual verificada e escopo
  `quality-test-only`.
- O resultado é um artifact interno. Esta etapa não publica, envia mensagens nem
  cria gasto externo.

## Gate local ainda pendente

No PC de casa, instalar as dependências opcionais com Python 3.12 e instalar
eSpeak NG separadamente pelo instalador oficial. Depois executar:

```powershell
$env:AUTOMATION_FOUNDRY_TEST_KOKORO='1'
.venv\Scripts\python.exe -m pytest tests/integration/test_kokoro_tts.py -q
```

O teste técnico não aprova a voz. Uma pessoa deve escutar amostras curtas e
longas das três vozes e registrar clareza, pronúncia de nomes/números, artefatos,
ritmo, estabilidade, consumo de VRAM e duração. Conteúdo monetizado só pode sair
do bloqueio após uma decisão explícita sobre o risco residual de proveniência;
a alternativa preferível é uma voz própria ou contratada com licença por voz.
