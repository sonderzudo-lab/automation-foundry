# Content Engine — passagem de bastão (2026-10-10)

Prompt para continuar em outra sessão de agente (Codex). Cole este arquivo ou peça ao
agente que o leia. Ele descreve o que foi decidido, o que foi feito e verificado, o que
**não** foi verificado e o próximo passo. Nada aqui substitui `AGENTS.md`, `ARQUITETURA.md`,
`ROADMAP.md` e a skill `.agents/skills/build-content-engine/SKILL.md`: leia-os primeiro.

## Atualização de 2026-10-10, depois da passagem

Este documento continua sendo o plano de trabalho. O que mudou desde que foi escrito:

- **Passo 1 concluído.** A run 12 falhou em A3 (`CONTENT_VISUALS_COUNT_MISMATCH`: 8 imagens
  exigidas, 5 no manifesto). Depois de duas correções, a run 13 atravessou A1→A8 com o
  conjunto novo: narração Qwen3 de 45,46 s, 6 imagens, legendas com 97,6 % de
  correspondência, edição `motion`, alinhamento v2 em `pass`, quatro gates aprovados, nada
  publicado. Vídeo: `D:\automation-foundry\storage\content-engine\run-13\assembly\video.mp4`.
  As imagens eram de outro tema; vale como teste técnico.
- **Commits novos:** `cd4684d` (o gate A2 recusa a aprovação enquanto o manifesto não
  serve para A3), `33412ae` (o manifesto define a quantidade de imagens dentro de uma faixa
  de 4 a 10 s por imagem), `2ffecc8` (roadmap com a run 13).
- **Manifesto de imagens:** `imports/content-visuals/manifest.json` voltou a declarar 6
  imagens (cobre narrações de 24 a 60 s); o de 5 ficou guardado na mesma pasta.
- **Passo 3, parte documental, concluída.** FLUX.2 klein 4B, Z-Image Turbo e Qwen-Image
  foram auditados em `docs/licensing/content-visuals.md`. Nenhum divulga os dados de treino,
  então a D4 mantém os três fora de conteúdo monetizado. Nada foi baixado.
- **Ainda pendente do proprietário:** a nota dele nos cinco itens da D1 para o vídeo da
  run 13 e a decisão escrita do passo 2 (riscos de licença da voz).
- Suíte: 842 passam, 13 pulados, mais as três falhas de instância única de sempre.

## Como trabalhar com o proprietário

- Ele não é técnico em mídia/ML. Explique em linguagem simples, recomende uma opção e peça
  a decisão; ele costuma responder "vou com sua recomendação".
- Ele julga resultado ouvindo, assistindo e lendo. Entregue arquivos para comparar (A/B)
  dentro da pasta do projeto e diga o **caminho absoluto completo**. Existem duas pastas
  `storage`: `C:\Users\PC\Desktop\Projetos_Dev\automation-foundry\storage` (do projeto,
  ignorada pelo Git, usada para experimentos) e `D:\automation-foundry\storage` (o
  `STORAGE_ROOT` real do pipeline). Essa ambiguidade já causou confusão.
- Peça autorização antes de baixar, instalar, apagar, publicar ou gastar. Downloads de
  modelos e reinícios do runtime foram autorizados caso a caso nesta sessão; isso não é
  autorização permanente.
- Commits: pequenos, um por mudança, sem atribuição de IA, na branch
  `chore/project-skills`. Nada foi enviado ao remoto. Não fazer merge em `main` nem abrir PR
  sem pedido explícito.

## Decisões do proprietário (2026-10-09/10)

| # | Decisão |
|---|---|
| D1 | Qualidade = nota dele no gate A7 em: fatos sem fonte, gramática, gancho, ritmo, relação imagem–texto. |
| D2 | Monetização **é** meta. Voz e imagens precisam de licença limpa antes de qualquer upload. |
| D3 | Nenhum número ou estudo sem fonte anexada na pauta. |
| D4 | **Não** aceitar ainda o risco de dataset de treino não divulgado para modelos de imagem. Visuais monetizados seguem com acervo próprio, stock ou geração externa com direitos declarados. Reavaliar depois da auditoria de imagem. |
| D5/D6 | Autorizou mudar o contrato do A1 e a métrica de alinhamento; o hook refinado vira a abertura narrada. |
| D7 | `diffusers` direto atrás de adapter, não ComfyUI. |
| D8 | API paga só se muito necessário e muito custo-benefício. O experimento E2 não justificou. |
| D9 | Haverá vários canais, alguns com vídeos longos e outros curtos. O sistema ainda não tem o conceito de canal (`channels` é tabela legada) e o formato long nunca foi validado. |
| Edição | Aprovou assistindo a edição "motion" (C2). |
| Voz | Prefere o Qwen3-TTS clonando a própria voz. Pediu para ligar no runtime. |

Pendente dele: decisão **por escrito** sobre os três riscos de licença da voz (dados de
treino não divulgados, licença dos pesos só no model card, titularidade do áudio) antes do
primeiro vídeo monetizado. Até lá o backend é `quality-test-only`.

## O que foi feito nesta sessão (commits em `chore/project-skills`)

| Commit | Mudança |
|---|---|
| `85ecd9b` | Métrica de alinhamento A4 v2: cada bloco de legenda é comparado com a retomada de fala mais próxima. Relatórios v1 seguem legíveis. |
| `07d42b2`, `a20dc32` | Limpeza da narração: seletores de variação, joiners, keycaps, bandeiras; hashtags não são mais narradas. |
| `54999ac` | Hook escrito antes do roteiro e montado em código como abertura narrada. |
| `17d2cbd` | Narração short validada em 100–150 palavras, até 3 reescritas, depois `CONTENT_SCRIPT_LENGTH_OUT_OF_RANGE` (retryable). Remove rótulos `[..]` e `(N palavras)`. |
| `d9e9a5e`, `3c3e267`, `ccaf704` | Fatos com fonte (`facts`) na pauta; regra em todos os prompts; `find_unsourced_claims` heurístico; campo no formulário e seções no gate A1. |
| `22aa6f9` | `opening_style` (5 estilos + `auto` por rotação) e `structure` (direct, myth_fact, list, story). |
| `f3954b0` | `CONTENT_ASSEMBLY_EDIT=motion`: zoom/deslocamento com `perspective`, crossfade de 0,4 s, cortes no início de frases. |
| `8854654` | Auditoria documental de Chatterbox PT-BR e Qwen3-TTS em `docs/licensing/content-tts.md`. |
| `31f96b5` | Backend `qwen3_tts_clone_quality_test` (subprocess em ambiente separado, offline, hashes conferidos). |
| `ba03133` | Roadmap: o adapter do roteiro não controla a janela de contexto do Ollama. |

`ROADMAP.md` foi atualizado em cada fatia e é a fonte mais detalhada do estado.

## Estado do runtime nesta máquina

- Runtime ligado pela tarefa `AutomationFoundryRuntime`. Reinício documentado:
  `automation-foundry-runtime stop` e depois `Start-ScheduledTask -TaskName AutomationFoundryRuntime`,
  sempre conferindo antes que não há run em execução. O painel em `127.0.0.1:8000` recarrega
  templates do disco mas mantém o Python antigo: mudou template ou Python do dashboard,
  reinicie logo em seguida.
- `.env` (não versionado) foi alterado nesta sessão:
  - `CONTENT_ASSEMBLY_EDIT=motion`
  - `CONTENT_TTS_BACKEND=qwen3_tts_clone_quality_test`, `CONTENT_TTS_VOICE_ID=owner-reference`,
    `CONTENT_TTS_LANGUAGE_CODE=pt` e os quatro caminhos `CONTENT_TTS_QWEN3_*`/`CONTENT_TTS_REFERENCE_*`.
  - Valores anteriores da voz: `kokoro_quality_test`, `pm_alex`, `p`.
- Fora do repositório, no D::
  - `D:\automation-foundry\labs\tts-qwen3\` — venv Python 3.11, `qwen-tts` no commit
    auditado, modelo `Qwen/Qwen3-TTS-12Hz-1.7B-Base` na revisão fixada.
  - `D:\automation-foundry\labs\tts-chatterbox\` — ambiente do Chatterbox (8,4 GB), não
    usado pelo pipeline; pode ser removido se o proprietário quiser.
  - `D:\automation-foundry\voice\owner-reference.wav` e `.txt` — referência normalizada da
    voz do proprietário e a transcrição. Dado pessoal: nunca versionar.
  - A gravação original dele está em `D:\automation-foundry\storage\experiments\e7-voz\referencia.mp3`,
    dentro do `STORAGE_ROOT`, onde aparece como órfã no inventário. Ele sabe; mover é decisão dele.
- Ollama: baixados `mistral-small3.2:24b`, `qwen3.6:27b`, `gemma4:31b` (cerca de 52 GB) para
  o E2. O pipeline continua em `qwen3:14b`. Licenças desses três não foram lidas na fonte.
- Pasta `docs/experiments/` (planilha cega do E2 preenchida) e `storage/experiments/`
  (vídeos e áudios de comparação) não estão commitadas.

## Resultados medidos

- **E1 alinhamento:** runs 4, 5 e 11 remedidas dão `pass` (desvio máximo 0,22 s). A run 11 não foi reexecutada.
- **E2 modelos de roteiro** (3 pautas × 4 modelos, avaliação cega dele): `qwen3:14b` 4,83;
  `qwen3.6:27b` 4,83; `mistral-small3.2` 4,75; `gemma4:31b` 4,58. Sem ganho que justifique
  trocar. Achado: a API OpenAI-compatível do Ollama não define `num_ctx`; os modelos maiores
  carregam com 131.072 tokens e estouram os 24 GB. Qualquer troca exige o adapter controlar o contexto.
- **E3 fatos:** frases marcadas caíram de 16 em 8 roteiros para 11 em 15; depois, com
  estilos de abertura, voltaram algumas afirmações numéricas sem fonte. O detector marca, não bloqueia.
- **Variedade:** estilo de abertura seguido em 15 de 19 gerações; aberturas com "Você" caíram
  de 9 em 12 para 4 em 13. A estrutura `story` não produz história e repete o hook.
- **E4/E5 edição:** `zoompan` treme (34–38 % de oscilação entre quadros); `perspective`
  fica em cerca de 2 %. Render 7 s → 20 s, arquivo 3,3 MB → 13 MB por short.
- **E7 voz:** mesma narração de 102 palavras — Kokoro 36 s; Chatterbox PT-BR 49 s (gera em
  41 s, 3,5 GB); Qwen3-TTS 40 s (gera em 112 s sem FlashAttention, 4,6 GB). Pelo adapter real: 108 s.
- Suíte: 834 passam, 13 pulados. Três testes de instância única (`test_beat_cli`,
  `test_runtime_supervisor`) falham **somente** porque o runtime está ligado nesta máquina.

## O que NÃO foi verificado

- Nenhuma run A1→A8 completa rodou com o conjunto novo (roteiro novo, fatos, estilos, voz
  Qwen3, edição motion, alinhamento v2). **Este é o primeiro passo.**
- O gate de alinhamento com a voz nova: ritmo e pausas mudaram.
- Geração com fatos reais (`facts`) no modelo real; só há teste com cliente mockado.
- Formato long em qualquer etapa. A narração Qwen3 não cabe no timeout atual do step para textos longos.
- A verificação de preparação do painel não inspeciona o backend Qwen3.
- O tamanho do hook (pedido até 25 palavras, frequentemente excedido) não é validado.
- Erros de português do `qwen3:14b` só são pegos pela leitura humana.
- Audição humana do vídeo da run 11 nunca aconteceu.

## Próximos passos, em ordem

1. **Feito (run 13).** Pedir ao proprietário que dispare um short pelo painel (estilo
   "Rotação automática", estrutura "Lista de itens") e acompanhar os gates A1, A2, A7, A8.
   Falta só a nota dele no vídeo.
2. Registrar por escrito a decisão dele sobre os riscos de licença da voz e, só então,
   discutir tirar o escopo `quality-test-only`.
3. **Parte documental feita; resultado: nenhum aprovado sob a D4.** Auditoria de licença
   de imagem (itens que reprovaram o FLUX.1 em
   `docs/licensing/content-visuals.md`): candidatos FLUX.2 klein 4B, Z-Image Turbo,
   Qwen-Image. Lembrar a D4: ele não aceitou o risco de dataset. Teste de geração só fora
   do pipeline, porque o A3 exige `commercial_use=True` por arquivo.
4. Legendas quebrando no meio da frase (A4) e imagens sem relação com o texto (A3) são os
   defeitos visíveis que sobraram.
5. Pendências menores: probe de preparação para o Qwen3, validação do tamanho do hook,
   controle de `num_ctx` no adapter do roteiro, estrutura `story`, conceito de canal (D9).

## Regras que continuam valendo

Nada é publicado, enviado, gasto ou apagado sem aprovação humana. Backends `disabled` por
padrão; backend novo = adapter + licença documentada em `docs/licensing/` + testes.
Segredos e dados pessoais fora do Git. Não clonar voz de terceiros nem usar voz embutida.
Relatar o que foi verificado e o que não foi, sem presumir.
