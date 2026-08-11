# Licenças e gate operacional da montagem

Status: `CONTENT_ASSEMBLY_BACKEND=disabled` por padrão. O valor opt-in
`ffmpeg_quality_test` aceita somente executáveis externos com SHA-256 fixado;
ele não é habilitado automaticamente nem autoriza publicação.

Esta é uma decisão técnica de produto, não aconselhamento jurídico. A licença e
as obrigações de redistribuição do FFmpeg dependem da configuração concreta do
build e das bibliotecas/codecs habilitados; o nome “FFmpeg” sozinho não fecha o
inventário.

## Contrato implementado

- Consome apenas WAV A2, manifesto e imagens A3 e ASS A4 registrados na mesma
  run e novamente conferidos por tamanho e SHA-256.
- Reconcilia quantidade, nome, digest, tamanho, resolução e direito comercial
  de cada imagem com o manifesto A3.
- Executa como step ordinal 5 na fila `cpu`; nenhuma etapa GPU ou upload é
  disparada.
- Exige do adapter provider, ferramenta/build, licença, direito comercial,
  H.264, AAC e confirmação de legendas queimadas.
- Valida `ftyp`, `moov`, `mdat`, duração do movie header, presença de tracks de
  vídeo/áudio e resolução antes de publicar o MP4 por rename atômico.
- Registra export local, duração, tempo de render, bytes, energia CPU estimada e
  custo externo zero. O resultado continua sujeito a revisão humana e nunca é
  publicado automaticamente.

O parser interno é um gate estrutural limitado. Ele não decodifica frames nem
substitui `ffprobe`; somente um teste real opt-in poderá provar que o arquivo é
reproduzível e que as legendas foram visualmente queimadas.

## Gate para habilitar FFmpeg

Antes de adicionar qualquer valor além de `disabled` em `Settings`:

1. fixar origem, versão, distribuição e SHA-256 dos binários `ffmpeg` e
   `ffprobe`;
2. registrar a saída completa de build/configuração e licenças dos codecs e
   bibliotecas incluídos;
3. documentar obrigações de LGPL/GPL aplicáveis à forma de instalação ou
   redistribuição adotada pelo projeto;
4. usar subprocess sem shell, argumentos separados, timeout e stderr limitado e
   redigido;
5. executar somente com paths verificados abaixo de `STORAGE_ROOT`, sem aceitar
   argumentos livres vindos de roteiro ou adapter;
6. validar com ffprobe H.264, AAC, resolução, duração e ausência de tracks ou
   anexos inesperados;
7. assistir amostras short e long, confirmando transições, sincronismo, texto
   queimado, legibilidade e ausência de cortes;
8. medir CPU, memória, disco temporário, duração e recuperação após interrupção.

## Inventário local validado em 2026-08-11

- Distribuição: Gyan FFmpeg `8.0.1-full_build-www.gyan.dev`, instalada pelo
  WinGet e invocada como processo externo; os binários não são copiados para o
  repositório ou bundle da aplicação.
- Origem declarada pelo pacote: FFmpeg commit `894da5ca7d`.
- Configuração relevante: `--enable-gpl`, `--enable-version3`,
  `--enable-static`, `--enable-libx264` e `--enable-libass`.
- Licença incluída no pacote: GPL v3. O projeto não redistribui esse build; uma
  eventual distribuição futura exige revisão jurídica e cumprimento das
  obrigações GPL e de cada biblioteca incluída.
- `ffmpeg.exe` SHA-256:
  `74db6c184a03dba2bdfe23e1a1f41cf5a8385bc1de6a7a1b26db1dc541abef93`.
- `ffprobe.exe` SHA-256:
  `55bb6c6289367ae2383efa86b26bf2596f8adb72ac747360eb13df162354161c`.
- Encoders/filtros usados: `libx264`, AAC nativo e `libass`; o adapter recusa
  build sem as flags esperadas e recusa qualquer divergência de hash.
- Execução: subprocess sem shell, `-nostdin`, argumentos separados, timeout,
  stderr não persistido e paths já verificados pelo contrato A5.
- Saída real: teste opt-in gerou MP4 short 1080×1920 com uma track H.264 e uma
  AAC, duração conferida por ffprobe e legenda ASS visualmente confirmada em
  frame extraído. A amostra usa áudio silencioso e imagem sintética de teste;
  não valida qualidade editorial, sincronismo de narração real ou vídeo long.

As páginas oficiais do FFmpeg explicam que componentes opcionais GPL tornam o
build GPL; a página da distribuição Gyan declara seus builds estáticos como
GPLv3. Fontes: <https://ffmpeg.org/legal.html> e
<https://www.gyan.dev/ffmpeg/builds/>. Este registro é técnico e não é
aconselhamento jurídico.

`ffmpeg`, `ffmpeg_local` e nomes equivalentes continuam recusados. Somente
`ffmpeg_quality_test`, com os dois hashes explícitos, pode ser selecionado. A
promoção para backend de produção ainda depende de amostras short/long reais,
revisão humana de áudio/sincronismo, política de instalação e decisão sobre
redistribuição.
