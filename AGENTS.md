# Regras do projeto (para agentes de código — Cursor)

## Contexto
Leia `ARQUITETURA.md` antes de QUALQUER tarefa. Ele é a fonte da verdade.
Trabalhe sempre em UM módulo por conversa. Não tente construir vários módulos de uma vez.

## Stack fixa (NÃO substitua)
Python 3.12 · FastAPI (async) · Celery + Redis · SQLAlchemy 2.0 + Alembic ·
Ollama (Qwen3/Gemma) · Kokoro TTS · SDXL/FLUX Schnell · faster-whisper · FFmpeg ·
sentence-transformers (embeddings locais). NÃO troque essas escolhas por conta própria.

## Convenções de código
- Type hints obrigatórios em toda função.
- Toda função que pode falhar (rede, GPU, IO, API externa) usa try/except com log estruturado.
- Tarefas Celery: idempotentes, com `soft_time_limit`, `time_limit` e retry exponencial.
- Toda etapa que usa GPU vai para a fila `gpu` (concorrência = 1). FFmpeg vai para `cpu`.
  APIs/scraping/upload vão para `io`.
- Nenhum segredo hardcoded. Tudo via `src/core/config.py` (pydantic-settings) e `.env`.
- Escreva testes pytest JUNTO com o código. Mocke serviços externos (Ollama, APIs, GPU).
  Não entregue módulo sem teste.
- Atualize a tabela `jobs` em toda etapa do pipeline (queued→running→done/failed).

## Licenças (CRÍTICO — não viole)
- TTS: SOMENTE Kokoro ou Fish Speech (Apache 2.0). NUNCA XTTS v2 ou F5-TTS (não-comerciais).
- Imagem: SOMENTE SDXL ou FLUX Schnell (Apache 2.0). NUNCA FLUX Dev (proíbe vender o output).

## Regras de produto (NÃO remova "para ganhar velocidade")
- SEMPRE existe um gate de revisão humana (`approve`) antes do upload. O sistema nunca
  publica sozinho sem um humano aprovar tópico/ângulo/thumbnail.
- O detector de similaridade (D3) BLOQUEIA publicação automática quando vídeos ficam
  parecidos demais. Não contorne esse bloqueio.
- Comentários (G1) nunca são postados sem aprovação humana.
- Não use gpt4free nem APIs não-oficiais/reverse-engineered.
- Não gere código que poste em massa sem variação entre vídeos.

## Dicas de contexto
- Referencie arquivos explicitamente com `@ARQUITETURA.md` e `@caminho/arquivo.py`.
- Ao integrar dois módulos, abra ambos no contexto e descreva o contrato (inputs/outputs).
