# Fase 6 — comparação dos candidatos a segundo módulo

**Status:** o proprietário aceitou a recomendação (candidato 4, relatórios e briefs recorrentes).
Tema, fontes e baseline manual ainda estão em aberto. Nada aqui está implementado.
**Objetivo do portão (ROADMAP, Fase 6):** o segundo módulo reutiliza o kernel sem copiar o
control plane e demonstra valor mensurável em um caso real.

As notas são julgamento do assistente, de 1 a 5, onde **5 é sempre melhor para o proprietário**
(mais valor, dados melhores, menos risco, menos esforço, menos manutenção). Elas não foram
medidas. Os pesos são iguais; mudar o peso de "valor" altera o ranking e é decisão sua.

## Restrições que filtram todos os candidatos

Derivadas de `AGENTS.md` e `ARQUITETURA.md`:

- somente APIs oficiais, respeitando termos, rate limits e privacidade;
- aprovação humana antes de enviar mensagem, gastar ou publicar; nenhum efeito financeiro;
- runtime local, sem credencial versionada, com kill switch por módulo;
- primeiro uma execução manual, só depois schedule;
- outreach em massa e autoposting estão no backlog adiado.

## O que o kernel já oferece (verificado no código)

Run/step/approval/artifact/metric/ledger/alert/schedule, dispatch durável, filas `gpu`/`cpu`/`io`,
Beat, cliente Ollama usado pelo A1, approvals com digest e página de revisão, export local.
**Lacuna:** `ConnectorObservation` e o painel de connectors/freshness existem, mas nenhum
adapter produz observações ainda ("Nenhuma observação de connector foi registrada" na home).
Qualquer candidato com dados externos seria o primeiro a exercitar isso.

## Matriz

| Candidato | Valor | Dados | Risco | Esforço | Manutenção | Reuso do kernel | Soma |
|---|---|---|---|---|---|---|---|
| 4. Relatórios e briefs recorrentes | 3 | 5 | 5 | 4 | 4 | 4 | **25** |
| 2. Monitor de preços e oportunidades | 4 | 2 | 4 | 3 | 2 | 4 | **19** |
| 1. Inteligência de mercado e concorrentes | 4 | 2 | 3 | 2 | 2 | 4 | **17** |
| 3. Pesquisa e qualificação de leads | 3 | 2 | 1 | 2 | 2 | 4 | **14** |

Justificativas curtas:

- **Dados:** relatórios podem usar dados que o sistema já possui e feeds abertos; os demais
  dependem de fontes externas cujo acesso oficial é limitado, exige credencial ou muda sem aviso.
- **Risco:** leads envolvem dados pessoais (LGPD) e a tentação de outreach; preços e concorrentes
  esbarram nos termos de plataforma se a coleta não for por API oficial.
- **Manutenção:** fontes externas mudam; um brief com fontes fixas e fakes de teste quebra menos.

## 4. Relatórios e briefs recorrentes

- **Hipótese:** um brief semanal gerado localmente, a partir de fontes declaradas, é aprovado com
  pouca edição e leva menos tempo de revisão do que produzi-lo à mão.
- **Métrica de sucesso (proposta, a calibrar):** em 4 execuções, ao menos 3 aprovadas sem reescrita
  substancial, com tempo de revisão menor que o baseline manual **que você precisa informar**.
  Acompanhar também: frescor das fontes, custo externo (esperado 0), tempo de GPU.
- **Primeira fatia:** execução manual com um tema e fontes fixas → `io` coleta → `gpu` redige via
  Ollama → `cpu` valida/exporta → approval de revisão → export local `published=false`.
- **Gates:** aprovação de revisão antes de concluir; nada é enviado. Kill switch do módulo.
- **Reuso:** mesmo padrão do A1 (bundle com hash + página de revisão). Primeiro produtor real de
  `ConnectorObservation`, que fecha a lacuna acima.
- **Limitação honesta:** o valor financeiro direto é pequeno; prova plataforma e economia de tempo,
  não receita.

## 2. Monitor de preços e oportunidades

- **Hipótese:** alertas de queda de preço em uma categoria definida geram ao menos uma decisão de
  compra ou venda vantajosa por mês.
- **Métrica:** alertas úteis / alertas totais; economia atribuída registrada no ledger como
  `attributed_value`, com fonte e confiança.
- **Primeira fatia:** consulta manual de uma fonte oficial → comparação com histórico → alerta local.
- **Bloqueios:** exige API oficial do varejista (credencial ou afiliação). Raspagem de páginas
  costuma violar termos; verificar caso a caso antes de qualquer código.
- **Gates:** nenhuma compra automática; o módulo só alerta.

## 1. Inteligência de mercado e concorrentes

- **Hipótese:** um digest de sinais públicos de concorrentes muda ao menos uma decisão por mês.
- **Métrica:** sinais marcados como acionáveis pelo proprietário / sinais entregues.
- **Primeira fatia:** poucas fontes oficiais ou feeds abertos → deduplicação → resumo local → revisão.
- **Risco:** ruído alto; sem API oficial para a maioria das páginas de concorrentes. Sobrepõe-se ao
  candidato 4, que pode evoluir para este adicionando fontes.

## 3. Pesquisa e qualificação de leads

- **Hipótese:** rascunhos qualificados reduzem o tempo por lead sem comprometer a qualidade.
- **Métrica:** tempo por lead, rascunhos aprovados sem edição, respostas (somente se você enviar).
- **Riscos:** dados pessoais e consentimento (LGPD), termos das plataformas, e a pressão natural
  para automatizar o envio, que o roadmap adia deliberadamente.
- **Gates:** todo rascunho precisa de aprovação humana; envio fica fora da primeira fatia.
- **Recomendação:** deixar por último.

## Recomendação

Começar pelo **candidato 4**, com o tema e as fontes que importam para você. Ele tem o menor risco e
o menor esforço, não precisa de credencial, dá testes determinísticos com fakes e é o primeiro
produtor real de observações de connector. Se o brief provar valor, o candidato 1 vira o mesmo
módulo com novas fontes, e o candidato 2 só entra se você tiver uma categoria e uma API oficial
concretas.

## Decisões que só o proprietário pode tomar

1. Qual tema/domínio o brief cobre e quem o lê.
2. Quanto tempo o brief leva hoje à mão (baseline da métrica).
3. Quais fontes são aceitáveis (feeds abertos, APIs com credencial, dados do próprio sistema).
4. Se o objetivo da fase pesa mais em receita ou em portfólio. Isso muda o peso de "valor".
