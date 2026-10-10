# Content Engine — contexto para discutir qualidade e variedade dos vídeos

Documento para iniciar uma conversa de estratégia em uma sessão nova. Descreve o que
existe de fato, o que foi medido na run #11 e na run #5, as amarras que não são
negociáveis e as decisões em aberto. Data: 2026-10-09. Nada aqui é um plano aprovado.

## Como usar

Cole este arquivo no início da sessão e peça, por exemplo:

> Com base neste contexto, proponha um plano priorizado para melhorar a qualidade e a
> variedade dos vídeos, separando o que depende de decisão minha, o que exige auditoria
> de licença e o que posso validar com um experimento pequeno na minha RTX 3090.

Pesquise fora (modelos, formatos, fontes) apenas o que muda rápido; para licença, leia o
model card e o texto da licença na fonte, não um resumo de buscador.

## Estado real em uma página

- Pipeline local A1→A8 funciona de ponta a ponta para o formato **short** (1080×1920,
  cerca de 25–40 s). O resultado é um MP4 revisável; **nada é publicado** (`published=false`).
- Não existem: upload, analytics pós-publicação, formato long validado, identidade de canal
  validada, narrativa com fontes, nenhum provedor de imagem/vídeo gerado pela aplicação.
- Os vídeos produzidos hoje são um **slideshow**: PNGs importados, divididos em partes
  iguais, com legenda queimada e a voz do Kokoro. Sem movimento, transição ou trilha.
- Defeitos observados na run #11 (abaixo) são sobretudo de **roteiro**, não de mídia.

## Pipeline atual

| Etapa | Fila | O que faz | Implementação | Duração medida |
|---|---|---|---|---|
| A1 roteiro | gpu | 4 chamadas ao LLM: ângulo, outline, roteiro, hook; extrai `narration` | Ollama `qwen3:14b` por API OpenAI-compatível; `think` desligado + sanitização | 55 s (run 5), 107 s (run 11) |
| Gate A1 | humano | Aprovar o roteiro integral (hash do bundle) | dashboard | — |
| A2 narração | gpu | WAV PCM mono 24 kHz | Kokoro 0.9.4, vozes `pf_dora`/`pm_alex`/`pm_santa`, CUDA, eSpeak NG externo | 9 s (run 5), 45 s (run 11) |
| Gate A2 | humano | Ouvir o WAV; mostra a quantidade exata de visuais | dashboard (opcional por `.env`) | — |
| A3 visuais | io | Importa PNGs de uma pasta com manifesto estrito (direitos + SHA-256 por item) | `local_assets_quality_test` | 0,2 s |
| A4 legendas | gpu | Timestamps por palavra → ASS karaokê | faster-whisper small (MIT), revisão fixada, snapshot local; fallback "texto aprovado" em cpu | 9–10 s |
| A5 montagem | cpu | Concat das imagens, escala/pad, legenda queimada, H.264/AAC 30 fps | FFmpeg 8.0.1 Gyan (GPLv3, externo, fixado por hash) | 6–10 s |
| A6 similaridade | cpu | Cosseno de unigramas + Jaccard de trigramas contra até 20 runs anteriores; bloqueia ≥ 0,85 | lexical, local | < 0,1 s |
| Gate de alinhamento | cpu | Diagnóstico RMS de 20 ms do WAV contra os eventos do ASS; só `pass` abre A7 | opcional por `.env` | 0,2 s |
| Gate A7 | humano | Assistir o MP4 final + relatório de originalidade | dashboard | — |
| Gate A8 | humano | Escolher a thumbnail entre os PNGs do A3 | dashboard | — |
| Export local | — | Manifesto + downloads reverificados | dashboard | — |

Uma run completa leva cerca de 3 minutos de máquina, dominados por A1. O tempo humano é
dominado pelos quatro gates.

## Contratos que qualquer melhoria precisa respeitar

- **Visuais (A3):** um visual a cada 6 s de áudio (teto 60), PNG completo validado,
  1080×1920 (short) ou 1920×1080 (long), com `provider`, `model_id`, `source_id`,
  `source_uri`, `license_id`, `commercial_use=True` e atribuição **por arquivo**. A ordem
  vem do manifesto; não há ligação semântica entre uma imagem e um trecho do roteiro.
- **Legendas (A4):** palavras com tempos monotônicos dentro da duração, idioma PT,
  similaridade mínima com o texto aprovado, proveniência e direito comercial declarados.
- **Montagem (A5):** MP4 H.264 + AAC, legenda queimada, validado por boxes/tracks/duração;
  cada imagem recebe `duração_total / n` (partes iguais).
- **Backends:** `Literal[...]` em `src/core/config.py`, `disabled` por padrão, selecionados
  por `get_configured_*_adapter` em `tts.py`, `visuals.py`, `captions.py`, `assembly.py`.
  Um backend novo = adapter novo + licença documentada em `docs/licensing/` + testes.
- **Filas:** `gpu` com concorrência 1, `cpu`, `io`. GPU única de 24 GB compartilhada com o
  Ollama; um modelo grande residente (ex.: `qwen3-coder:30b`, ~23 GB) afeta a latência.

## Restrições não negociáveis (AGENTS.md e skill do Content Engine)

- Nada é publicado, enviado, gasto ou apagado sem aprovação humana.
- Somente variantes **comercialmente permitidas e documentadas**; proveniência e direitos
  rastreados para visuais, áudio, modelos e datasets.
- Runtime local-first; modelos por adapters da aplicação, não por MCP/plugin.
- Segredos fora do Git; credenciais por plataforma; upload inicial sempre privado.
- Preservar o gate editorial, o bloqueio de similaridade e a variação real (nada de
  templates genéricos em massa).
- Usar API paga ou externa é **gasto/envio de dados** e exige decisão explícita.

## Hardware

Ryzen 9 5950X, RTX 3090 (24 GB), RAM cerca de 32 GB (a confirmar). Storage e modelos do
Ollama em `D:`. Windows 11. Ainda não há medição de VRAM sustentada com mais de um modelo.

## Licenças e proveniência hoje

| Item | Situação |
|---|---|
| Kokoro (engine/pesos) | Apache-2.0; **proveniência individual das vozes PT-BR não documentada** → marcado "não aprovado para monetização" |
| eSpeak NG | GPL-3.0-or-later, externo ao pacote |
| faster-whisper small | MIT, revisão fixada |
| FFmpeg (build Gyan) | GPLv3, binário externo fixado por hash; política de redistribuição pendente |
| Imagens atuais | geradas por IA via OpenAI/Codex em 2026-08-22, com declaração do operador; reutilizadas entre runs |
| FLUX.1 | **bloqueado** no projeto até auditoria de licença |
| Provedores de stock | não integrados |

Isto é decisão técnica de produto, não aconselhamento jurídico.

## Evidência de qualidade observada (run #11, 2026-10-09)

Pauta: "Por que cortar uma hora de sono custa mais do que parece na produtividade".

1. **Afirmações numéricas sem fonte:** "dobra a chance de cometer erros críticos" e "custa
   três vezes mais tempo". O modelo as inventou; nenhuma etapa verifica fatos.
2. **Emoji no hook** e um caractere residual (U+FE0F) no fim da narração: a sanitização de
   emojis não cobre seletores de variação.
3. **Gramática:** "Por que?" no início de frase onde cabia "Por quê?". Nas legendas
   (saída do ASR) o trecho aparece como "porque"; isso é o que o ASR transcreveu, não
   foi confirmado por audição.
4. **Roteiro curto:** 89 palavras (~25,7 s), abaixo da faixa de 100–150 do prompt.
5. **Gate de alinhamento bloqueou a run** por `BLOCK_ONSET_OFFSET` (4,18 s). Medição
   independente indica falso positivo: o diagnóstico une pausas < 0,30 s, e a pausa real
   era de ~0,24 s; o bloco de legenda começava exatamente na retomada da fala. A run #5
   (22/08) passou pelo mesmo gate com o mesmo adapter, então a métrica é sensível à
   segmentação, não ao modelo.
6. Latência do A1 dobrou (55 s → 107 s) quando um modelo de 30B estava residente.

## Gargalos de qualidade e variedade (hipóteses, em ordem de impacto estimado)

1. **Roteiro** (maior impacto, menor custo): sem pesquisa nem fontes; saída em texto livre
   sem validação de afirmações; modelo de 14B; um único estilo de prompt por formato.
   Ideias: etapa de pesquisa com fontes citáveis, saída estruturada com lista de
   afirmações verificáveis, passe de crítico, filtro de emojis/pontuação, variação real de
   estrutura (listas, mito vs. fato, história curta), e avaliar um modelo local maior.
2. **Visuais:** slideshow estático com imagens reaproveitadas e sem relação com o texto.
   Ideias: prompts por cena derivados do roteiro, movimento (zoom/pan), b-roll licenciado,
   consistência de estilo por série.
3. **Voz:** qualidade PT-BR do Kokoro e, principalmente, a proveniência das vozes.
4. **Edição:** ritmo fixo (partes iguais), sem transições, trilha ou ênfase por palavra.
5. **Diagnóstico de alinhamento:** corrigir a métrica de início de bloco antes de depender
   dela como gate de qualidade.

## Decisões em aberto (são do proprietário)

- Qual é o objetivo da qualidade: retenção, aprovação sua no A7, monetização? Qual rubrica?
- Formato: só short ou também long? Qual nicho e persona? Série ou vídeos avulsos?
- Orçamento: uso de API paga/externa para roteiro é aceitável? Quanto tempo de GPU por vídeo?
- Monetização é meta? Se sim, a voz e os visuais precisam de licença limpa **antes** de
  qualquer upload.
- Quanto tempo humano de revisão por vídeo é aceitável (quatro gates hoje)?
- Aceita ComfyUI como serviço local atrás de um adapter (com nodes e modelos fixados por
  hash), ou prefere `diffusers` direto em Python?

## O que verificar fora antes de decidir

- Estado atual de modelos de imagem, vídeo e voz que rodem em 24 GB e falem PT-BR; o
  conhecimento do assistente que ajudou a preparar este arquivo vai até junho de 2026.
- Licença de cada candidato (código, pesos, saídas), e se o uso comercial exige atribuição.
- Para ComfyUI: quais custom nodes um workflow exige, a procedência deles e como a VRAM é
  liberada entre etapas.

## Ressalvas deste documento

- Durações vêm de uma run por tipo de situação; não são benchmarks.
- O `ROADMAP.md` dizia que o adapter faster-whisper "precisa atravessar uma run real
  A1→A8". O banco mostra a run #5 (22/08) usando esse adapter, com os quatro gates
  aprovados e `published=false`; a frase foi corrigida junto com este documento.
- A afirmação de que a métrica de alinhamento é um falso positivo vem de análise do
  relatório e do áudio; não houve audição nem revisão visual do vídeo da run #11.

## Onde olhar no código

- Roteiro: `src/pipeline/script_gen.py`, `src/pipeline/a1_executor.py`
- TTS: `src/pipeline/tts.py`, `docs/licensing/content-tts.md`
- Visuais: `src/pipeline/visuals.py`, `docs/licensing/content-visuals.md`
- Legendas e alinhamento: `src/pipeline/captions.py`, `caption_alignment.py`,
  `caption_alignment_gate.py`, `docs/licensing/content-captions.md`
- Montagem: `src/pipeline/assembly.py`, `docs/licensing/content-assembly.md`
- Similaridade: `src/intelligence/similarity.py`
- Gates: `narration_review.py`, `final_review.py`, `thumbnail_review.py`
- Diagnóstico de preparação: `src/pipeline/readiness.py`
- Configuração dos backends: `src/core/config.py`
