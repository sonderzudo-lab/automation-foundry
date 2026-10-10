# Migração local de storage e modelos

O código e o ambiente virtual permanecem no C:. A raiz de artefatos, estado do runtime,
logs e backups é configurada por `STORAGE_ROOT`; o snapshot de legendas usa
`CONTENT_CAPTION_MODEL_PATH`. Os registros de artifacts no banco usam caminhos relativos,
portanto a troca dessas raízes não exige atualizar o banco.

## Procedimento

1. Verificar um bundle existente com `verify-backup`; não restaurar o banco para mover arquivos.
2. Confirmar que não há runs `running`/`queued` nem backup em execução. Parar cooperativamente
   o runtime e esperar que sua tarefa de autostart termine.
3. Copiar o storage completo para o destino, preservando a origem, sem `/MIR` ou `/MOVE`.
   Copiar novamente arquivos operacionais alterados desde a primeira cópia; Beat e logs podem
   mudar enquanto o runtime estiver ativo. Comparar todos os arquivos por SHA-256 com ele parado.
4. Alterar apenas as duas raízes no `.env`, com caminhos absolutos. Os scripts Windows usam
   `storage-paths.ps1` para consultar o mesmo parser da aplicação, sem executar o conteúdo do
   `.env` como PowerShell. Reiniciar a tarefa faz os novos logs e a trava usarem o destino.
5. Com Ollama encerrado, copiar somente sua pasta `models` para o destino e comparar hashes.
   Configurar `OLLAMA_MODELS` no ambiente do usuário e reiniciar o aplicativo com o novo valor.
   Conferir também o diretório salvo no aplicativo: Ollama 0.35.0 nesta máquina sobrescreveu
   a variável do processo servidor com `settings.models` do seu banco local. Validar o valor
   efetivo no processo `ollama.exe serve`, além de listar os modelos pela API.
   Não copiar chaves privadas da pasta pai. A configuração segue a
   [documentação oficial do Ollama](https://docs.ollama.com/faq#how-do-i-set-them-to-a-different-location).
6. Iniciar `AutomationFoundryRuntime` pelo Agendador. Validar status, `/health`, `/storage`,
   inventário sem novos `missing`/`unsafe` e leitura de artefatos com hash verificado.
7. Preservar as origens até uma decisão separada de limpeza. Copiar não libera espaço no C:.

## Docker Desktop

O disco virtual do Docker é uma migração separada. Com o runtime parado e sem backup em curso,
usar **Settings → Resources → Advanced → Disk image location**, quando disponível para o backend,
e escolher uma pasta própria no D:. Deixar o Docker realizar a movimentação; não mover o VHDX
enquanto ele estiver ativo. A disponibilidade depende do backend instalado, conforme as
[configurações oficiais do Docker Desktop](https://docs.docker.com/desktop/settings-and-maintenance/settings/).
Depois conferir o engine, PostgreSQL, Redis e o runtime. O bundle verificado deve permanecer
acessível durante todo o processo.

## Retorno ao caminho anterior

Se a validação inicial falhar, manter o runtime parado, reverter as duas variáveis do `.env`
para a origem preservada e iniciar novamente a tarefa. Para Ollama, encerrar o aplicativo,
restaurar o valor anterior de `OLLAMA_MODELS` (ausente nesta máquina), devolver o diretório
de modelos salvo no aplicativo para `%USERPROFILE%\.ollama\models` e reabrir.
Se já houver novos artefatos no D:, primeiro reconciliar e copiar esses arquivos de volta com
verificação de hashes; a cópia antiga no C: não acompanha gravações feitas depois da troca.
Não restaurar o banco nem sobrescrever arquivos divergentes para contornar a falha.

## Evidência da execução em 2026-10-09

- Storage ativo: `D:/automation-foundry/storage`; snapshot de legendas:
  `D:/automation-foundry/storage/models/captions/faster-whisper-small-536b066`.
- Cópia final com runtime parado: 92 arquivos, todos iguais à origem por SHA-256.
- Bundle existente `20261009T224802Z-1dc5fcc4` verificado independentemente no C: e no D:
  (`postgres-custom`, 64 arquivos, 500.435.575 bytes). Nenhum novo bundle do projeto foi criado.
- Os 45 artifacts do PostgreSQL conferiram em tamanho e SHA-256 no D:. O snapshot de legendas
  passou pelo verificador de hashes fixados do próprio adapter. Inventário sem `missing` ou
  `unsafe`; arquivos sem registro, como os modelos, já existiam antes da migração.
- Ollama: 23 arquivos, 63.521.033.093 bytes (59,16 GiB), com SHA-256 idêntico nos dois discos;
  os blobs também conferiram com seus nomes endereçados por conteúdo. `OLLAMA_MODELS` persistido
  no usuário e diretório salvo no aplicativo apontam para `D:\automation-foundry\ollama\models`.
  Foi necessário encerrar os dois processos ociosos iniciados durante a validação e atualizar
  somente `settings.models`, em transação, preservando as demais configurações. Antes disso,
  um backup SQLite consistente foi salvo localmente em
  `%LOCALAPPDATA%\Ollama\db.before-storage-migration-20261009.sqlite` (fora do Git).
  Após reiniciar, o ambiente do servidor confirmou o D:, `/api/tags` retornou cinco modelos
  e `/api/show` leu os metadados de `qwen3:14b`. Inferência completa não foi executada.
- Runtime reiniciado pela tarefa `AutomationFoundryRuntime`, com os três workers, Beat e
  dashboard ativos, logs novos no D: e PostgreSQL/Redis preservados. `/`, `/storage`, `/health`
  e `/runs/4/local-export/manifest.json` responderam HTTP 200; o coletor de saúde retornou
  `pass` para CPU, memória, disco, banco, Redis, workers, Beat e GPU. `doctor --services`:
  oito verificações `PASS`.
- Testes de resolução de caminhos, runtime, backup e retenção: 46 passaram e dois foram
  ignorados; Ruff e análise de sintaxe PowerShell passaram. Os dois scripts de status também
  foram executados a partir de outro diretório e resolveram o `.env` correto.
- Pendências: movimentação do Docker pela interface, criação de um novo backup agendado no D:,
  login/reboot real com a nova raiz e eventual remoção dos originais no C:. As origens e a pasta
  preexistente `D:\content-engine` foram preservadas.
