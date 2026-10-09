# Recuperação local e troubleshooting

Este guia cobre falhas locais comuns do Automation Foundry sem alterar dados de
automação. Ele não publica conteúdo, não envia mensagens, não instala modelos e
não executa expurgo. O banco permanece a fonte da verdade; Redis só transporta
entregas que já foram persistidas.

## Antes de agir

Abra um PowerShell na raiz do repositório e ative o ambiente virtual já criado:

```powershell
.venv\Scripts\Activate.ps1
```

Verifique o estado local, sem fazer mutações:

```powershell
automation-foundry doctor --services
automation-foundry-runtime status --json
```

`doctor --services` retorna código `1` quando Redis ou Ollama não respondem em
loopback. Isso é um diagnóstico seguro, não uma corrupção de run. O comando
`status` não expõe PIDs e mostra apenas o runtime que foi iniciado pelo
supervisor.

## Redis indisponível

Se o diagnóstico mostrar Redis indisponível, confirme primeiro a configuração
local em `.env`: `REDIS_URL` e `CELERY_RESULT_BACKEND_URL` devem apontar para
loopback. Para o serviço Redis previsto pelo Compose, inicie somente esse
serviço:

```powershell
docker compose up -d redis
automation-foundry doctor --services
```

Não use `docker compose down` como procedimento de recuperação: ele não é
necessário para Redis e pode afetar outros serviços locais. Se uma publicação de
dispatch falhou durante a queda, a entrega durável permanece `pending`; depois
que Redis voltar, o dispatcher poderá republicar a mesma entrega. Não crie uma
nova run manual para compensar a queda sem antes observar a run existente no
dashboard.

O checkpoint descartável verificou a sequência: falha de publicação, retorno do
Redis, republicação da mesma entrega, uma única run/step e rejeição de entrega
duplicada terminal. O resultado real depende do PostgreSQL local estar
configurado para o runtime concorrente.

## Ollama indisponível

O Ollama não é gerenciado pelo Compose nem pelo supervisor. Quando ele estiver
fechado, `doctor --services` deve indicar a falha de forma redigida; uma run A1
que tente chamá-lo falha com `CONTENT_LLM_UNAVAILABLE`, sem endpoint no erro e
sem artifact ou approval parcial.

Para iniciar o serviço local instalado pelo operador, use:

```powershell
ollama serve
```

Em outro PowerShell, confirme apenas a saúde:

```powershell
automation-foundry doctor --services
```

Não repita uma run falha automaticamente. Revise o erro e use o retry explícito
do dashboard somente quando a entrada ainda fizer sentido editorialmente. O
teste manual de queda e retorno deste computador confirmou a falha segura e o
retorno HTTP local saudável, com cinco modelos já instalados; a qualidade de um
modelo específico continua uma decisão humana separada.

## Reinício durante uma task

O supervisor deve ser parado de forma cooperativa quando possível:

```powershell
automation-foundry-runtime stop
automation-foundry-runtime start
```

O `start` executa o preflight e as migrations antes de abrir workers. Não mate
processos por PID para recuperar uma run: o status público deliberadamente não
os revela e o Job Object é apenas fallback do supervisor.

Se uma interrupção ocorreu enquanto uma task estava `running`, aguarde a lease
de dispatch expirar e acompanhe a mesma run no dashboard. O reclaim registra
uma tentativa interrompida com erro estruturado `INTERRUPTED_ATTEMPT` antes de
executar uma nova tentativa. Uma entrega terminal repetida não cria step novo.
Essa propriedade foi simulada com SQLite. O stop/start cooperativo dos três
workers em idle passou contra o volume PostgreSQL doméstico, mas o ensaio de
interromper um worker durante uma task real continua **bloqueado local**.

## Falta de espaço em disco

Não tente encher o disco do computador para testar este caso. A escrita do bundle
A1 converte `ENOSPC` em `CONTENT_STORAGE_WRITE_FAILED`, retryable e redigido,
sem registrar artifact ou approval parcial. Libere espaço fora de `STORAGE_ROOT`
ou mova a raiz para um volume local adequado; em seguida verifique o inventário:

```powershell
automation-foundry storage-inventory --json
```

O inventário é somente leitura. Ele não remove órfãos nem diretórios vazios. O
teste com filesystem realmente cheio continua **bloqueado local**, porque não
foi criado um volume descartável seguro nesta máquina.

## Quando parar e investigar

Pare a recuperação e não dispare outra execução se qualquer uma destas condições
ocorrer:

- o dashboard mostra uma approval pendente para a run em questão;
- o inventário classifica um artifact como `unsafe`, `missing` ou
  `retention_hold` inesperadamente;
- `doctor` falha para PostgreSQL, ou o runtime configurado ainda usa SQLite em
  cenário concorrente;
- a mesma run mostra uma transição que não corresponde ao estado observado.

Nesses casos, preserve o banco e o storage como evidência local. A recuperação
segura começa pela inspeção, não por recriar runs, remover arquivos ou reiniciar
serviços indiscriminadamente.
