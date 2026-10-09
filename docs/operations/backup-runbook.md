# Runbook de backup e restauração

**Status:** a rotina semanal de backup está implementada e instalada desde 2026-10-09; a restauração
continua sendo um procedimento manual e deliberado. As quatro decisões do proprietário (seção 7)
seguem em aberto e a rotina usa os padrões mais seguros: destino local, nada é apagado e os pesos de
modelo continuam nos bundles.

## 1. O que o backup cobre

Fonte: `src/operations/backup.py`. Cada bundle é uma pasta em `storage/backups/<id>/` com:

| Arquivo | Conteúdo |
|---|---|
| `database.dump` | `pg_dump --format=custom` do banco, executado dentro do container `postgres` do Compose |
| `artifacts.zip` | tudo em `storage/` **exceto** `backups/`, `runtime/` e `.gitkeep` |
| `configuration.json` | configuração permitida e **sem segredos** |
| `manifest.json` | tamanhos e SHA-256 de cada arquivo; o bundle só é publicado depois de verificado |

**O que o bundle não contém, e por isso precisa de outra cópia:**

- **O arquivo `.env`.** A configuração salva é saneada: senhas e chaves não entram. Numa
  recuperação total, o `.env` precisa ser recriado a partir de um cofre de senhas.
- **O código.** Está no Git, mas hoje a branch `chore/project-skills` tem commits que nunca foram
  enviados a nenhum remoto; este checkout é a única cópia.
- **A tarefa do Agendador** (`AutomationFoundryRuntime`): reinstale com
  `scripts\windows\runtime-autostart.ps1 install`.

## 2. Estado atual (medido em 2026-10-09)

| Item | Valor |
|---|---|
| Bundles existentes | 4: dois de 22/08/2026 (SQLite e PostgreSQL) e dois de **09/10/2026** (`20261009T161603Z-350d488d`, feito à mão, e `20261009T162433Z-a17da219`, feito pela tarefa; PostgreSQL, 64 arquivos, cerca de 480 MB cada) |
| Banco vivo | 11 MB, 10 runs, migration `20260811_0022` |
| `storage/` | 522 MB, dos quais **464 MB são `storage/models`** (pesos de modelo) |
| Disco `C:` | 88% usado, cerca de 115 GB livres |
| `verify-backup` nos 3 bundles | `ok` |
| Restauração do bundle PostgreSQL de agosto num PostgreSQL 17 descartável | sem erro; 31 tabelas, migration `20260811_0022`; container removido; banco vivo intocado |
| Backup de 09/10: tempo e parada | 26,5 s para criar; runtime parado por cerca de 1 minuto no total |
| Restauração do bundle de 09/10 num PostgreSQL 17 descartável | sem erro; `runs=10`, `approvals=15`, `artifacts=45`, `schedules=3` e migration `20260811_0022`, **idênticos ao banco vivo** |
| Segredos no bundle de 09/10 | nenhuma chave com nome de segredo e nenhuma string de conexão com credenciais em `configuration.json` |

## 3. Restrições que moldam a rotina

1. **O backup exige o runtime parado.** `create_backup` recusa se o supervisor está ativo
   (`backup.py:114`). Não há como agendar um backup sem uma janela de parada.
2. **Cada backup é completo e inclui os pesos de modelo.** `storage/models` não é excluído, então
   cada bundle teria cerca de 500 MB, quase todo de arquivos que mal comprimem e que são
   reproduzíveis a partir do snapshot fixado.
3. **O mesmo disco.** O destino padrão é `storage/backups`, no `C:`. Uma falha do disco leva dados e
   backups juntos. `--destination` aceita outro diretório (por exemplo, um disco externo).
4. **A restauração só entra em storage vazio e limpa o banco.** `restore-backup` exige que não haja
   arquivos de artefato em `storage/` e roda `pg_restore --clean --if-exists` no banco configurado.
5. **`restore-backup` não serve para ensaio.** Ele sempre age no serviço `postgres` do Compose deste
   repositório e exige que o nome do banco do bundle seja igual ao configurado. Apontá-lo para
   "outro lugar" não é possível; executá-lo é **limpar o banco vivo**. O simulado de restauração
   é, portanto, manual (seção 6).
6. **Apagar backups antigos é apagar dados.** Qualquer rotação precisa da sua autorização
   explícita, conforme a regra do projeto sobre exclusão.

## 4. Rotina proposta de backup

Frequência sugerida: **semanal, domingo às 22:00 (Brasília)**, antes do brief de segunda às 08:00.
A parada dura de 1 a 3 minutos e nada pendente se perde: runs aguardando aprovação ficam no banco.

Procedimento manual (a base de qualquer automação):

1. **Pré-checagem:** confirme que não há run `running` ou `queued` no dashboard. Aprovações
   pendentes não impedem o backup.
2. **Parar o runtime:**
   ```powershell
   .venv\Scripts\automation-foundry-runtime.exe stop
   ```
   Aguarde `automation-foundry-runtime status` mostrar `runtime: stopped`.
3. **Criar o bundle** (opcionalmente fora do disco do sistema):
   ```powershell
   .venv\Scripts\automation-foundry.exe backup --json
   .venv\Scripts\automation-foundry.exe backup --destination E:\af-backups --json
   ```
4. **Verificar o bundle gerado**, de forma independente da criação:
   ```powershell
   .venv\Scripts\automation-foundry.exe verify-backup <caminho-do-bundle> --json
   ```
5. **Religar o runtime** (mesmo se o backup falhar):
   ```powershell
   Start-ScheduledTask -TaskName AutomationFoundryRuntime
   ```
6. **Registrar** a data e o ID do bundle em um lugar seu.

### Automação implementada

`scripts/windows/runtime-backup.ps1` e a tarefa `AutomationFoundryBackup` (por usuário, sem
elevação, domingo 22:00 no horário local, `StartWhenAvailable`, limite de 2 h):

```powershell
powershell -File scripts\windows\runtime-backup.ps1 install
powershell -File scripts\windows\runtime-backup.ps1 install -Destination E:\af-backups
powershell -File scripts\windows\runtime-backup.ps1 status
powershell -File scripts\windows\runtime-backup.ps1 uninstall
Start-ScheduledTask -TaskName AutomationFoundryBackup
```

O que a tarefa faz, na ordem:

1. Confere que o destino existe e tem ao menos 5 GB livres e que o Docker responde.
2. Se o runtime estava ligado, espera que não haja run `running` ou `queued` (até 6 verificações,
   uma a cada 5 min); se continuarem ativas, **pula o backup** em vez de interromper trabalho.
3. Cria `storage/runtime/backup.lock` (a tarefa de autostart espera por ele, para não religar o
   runtime no meio do backup), para o runtime e espera a parada.
4. Cria o bundle e o verifica de forma independente com `verify-backup`.
5. Sempre remove a trava e, **somente se foi ela quem parou o runtime**, o religa pela tarefa de
   autostart. Se você já tinha parado o runtime, ele continua parado.
6. Em falha, abre o alerta `Backup semanal falhou` (erro, automação `platform-health`); no próximo
   backup bem-sucedido o alerta é resolvido sozinho.

O registro fica em `storage/runtime/logs/backup.log`. A tarefa **nunca apaga bundles**. Se o PC
estiver desligado no domingo, ela roda na próxima oportunidade; a tolerância de 12 h do brief
semanal cobre uma parada de 1 minuto na segunda-feira.

Validação (2026-10-09): caminho de falha com destino inexistente (aborta antes de tocar no runtime,
sai com código 1 e abre o alerta); caminho de sucesso pela própria tarefa em 46 s (runtime parado e
religado, bundle de 477 MB verificado, alerta resolvido, trava removida, schedule do brief intacto).
O disparo no horário real, no domingo, ainda não foi observado.

## 5. Procedimento de restauração (recuperação real)

Use somente depois de perda ou corrupção reais. A decisão é sua.

1. **Preparar:** instale as dependências, recrie o `.env` a partir do cofre de senhas, suba o Docker
   e confirme `docker compose config --quiet`.
2. **Parar o runtime** e garantir que `storage/` não tem artefatos (a restauração recusa se tiver).
   Mantenha `storage/backups/` e `storage/runtime/`; eles não contam.
3. **Escolher o bundle** e verificá-lo:
   ```powershell
   .venv\Scripts\automation-foundry.exe verify-backup <bundle> --json
   ```
4. **Restaurar**, com a confirmação exata do ID (a CLI cria antes um bundle de segurança do estado
   atual e o informa no resultado):
   ```powershell
   .venv\Scripts\automation-foundry.exe restore-backup <bundle> --confirm "RESTORE <backup-id>" --json
   ```
5. **Conferir:** `alembic current` deve mostrar o `head`; `automation-foundry doctor --services`;
   depois ligue o runtime e confira a home, `/health`, o número de runs e as aprovações.
6. **Guardar** o bundle de segurança gerado no passo 4 até ter certeza de que o estado restaurado
   está correto.

## 6. Simulado de restauração (seguro, sem tocar nos dados)

Executado em 2026-10-09 com o bundle PostgreSQL de agosto. Repita depois de qualquer mudança no
código de backup e de tempos em tempos. **Nunca use `restore-backup` para isso.**

```bash
docker run -d --rm --name af-restore-drill -e POSTGRES_PASSWORD=<valor-aleatorio> \
  -e POSTGRES_USER=drill -e POSTGRES_DB=drill postgres:17-alpine
docker exec -i af-restore-drill pg_restore --exit-on-error --single-transaction \
  --no-owner --no-privileges --username drill --dbname drill < <bundle>/database.dump
docker exec af-restore-drill psql -U drill -d drill -Atc "select version_num from alembic_version"
docker exec af-restore-drill psql -U drill -d drill -Atc "select count(*) from runs"
docker stop af-restore-drill
```

Critério de sucesso: `pg_restore` sem erro, `alembic_version` igual ao `head` esperado, contagens
compatíveis com o momento do bundle e nenhum container sobrando. O container usa uma senha de teste
própria e não toca o `automation-foundry-postgres`.

## 7. Decisões que só o proprietário pode tomar

1. **Frequência e horário.** Semanal, domingo 22:00 (Brasília), é só uma sugestão.
2. **Destino fora do disco do sistema.** Um disco externo resolve a falha do `C:` sem efeito
   externo. Enviar o bundle para a nuvem é uma ação externa com conteúdo privado (artefatos,
   banco): exigiria criptografia e a sua autorização explícita, e não está proposto aqui.
3. **Retenção.** Sem rotação, 52 backups de cerca de 500 MB somam perto de 26 GB por ano. Apagar
   antigos exige autorização; a proposta é **manter todos** até você decidir uma política.
4. **Excluir `storage/models` dos bundles.** Reduziria cada bundle de cerca de 500 MB para cerca de
   50 MB. Em troca, uma restauração exigiria reinstalar o snapshot fixado dos modelos, o que o
   projeto já sabe verificar por SHA-256. É uma mudança de código pequena e testável em
   `_EXCLUDED_STORAGE_NAMES`.

## 8. O que ainda não foi validado

- Um destino **fora do disco do sistema**: o bundle de 09/10 está em `storage/backups`, no mesmo `C:`.
- `restore-backup` de ponta a ponta num ambiente novo, por desenho só possível numa recuperação real.
- O primeiro disparo no horário agendado (domingo 11/10, 22:00) e o comportamento da tarefa de
  autostart se o PC ligar durante um backup atrasado.
- A cópia para um destino externo.
