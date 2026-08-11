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
