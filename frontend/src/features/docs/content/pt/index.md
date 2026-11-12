O Skynet melhora um código que você já tem, ou um código que ele escreve para você. O Skynet é o Optimize Anything: toda execução otimiza um repositório. Você fornece um repositório, um conjunto de casos e um avaliador. O Skynet propõe novas versões do repositório, pontua cada uma com o seu avaliador e mantém a melhor versão.

## O que o Skynet otimiza

Toda execução otimiza um repositório. Você pode começar de duas formas:

| Início                     | O que acontece                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------- |
| Usar um repositório        | O Skynet altera os arquivos nos caminhos que você permite, em um repositório do GitHub que você conecta. |
| Criar um repositório novo  | Você fornece uma tarefa e dados. O Skynet cria um novo repositório do GitHub na sua conta e escreve uma primeira solução. |

O código no repositório pode ser qualquer coisa: uma aplicação, um serviço, um script ou uma biblioteca. Ele pode usar qualquer biblioteca.

Toda execução usa o mesmo contrato. O seu avaliador define `score(repo_path, case)`. Ele retorna uma pontuação e um feedback escrito. O otimizador lê o feedback para melhorar a próxima versão.

> [!NOTE]
> Cada versão roda em uma sandbox. O agente de código que escreve as versões não vê o código do avaliador nem os valores esperados.

## Um produto, vários motores

O **Auto** é o padrão de toda execução. Ele testa GEPA, AutoResearch, Meta-Harness, AutoSaddler e ShinkaEvolve e depois continua o GEPA a partir da melhor versão. Você também pode escolher um motor: GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler ou ShinkaEvolve. Leia [Como a otimização funciona](/docs/concepts/optimization) e [Motores](/docs/concepts/engines).

## Como funciona uma execução

1. Você descreve a tarefa e escolhe um repositório ou pede ao Skynet que crie um.
2. Você fornece os casos. O Skynet os divide em conjuntos de treino, validação e teste.
3. Você seleciona um otimizador, os modelos e um orçamento.
4. Você escreve o avaliador. Uma verificação de configuração o executa na versão inicial.
5. O Skynet executa a otimização e mostra os resultados.

O Skynet seleciona as versões com base nos casos de validação. Ele informa a pontuação final com base nos casos de teste. Os motores nunca veem os casos de teste.

## Por onde começar

| Se você quer                         | Leia                                                  |
| ------------------------------------ | ----------------------------------------------------- |
| Iniciar sua primeira execução        | [Início rápido](/docs/quickstart)                     |
| Entender como a otimização funciona  | [Como a otimização funciona](/docs/concepts/optimization) |
| Escolher um motor                    | [Motores](/docs/concepts/engines)                     |
| Preparar seus dados                  | [Conjuntos de dados e divisões](/docs/concepts/datasets) |
| Escrever um avaliador                | [Métricas e avaliadores](/docs/concepts/scorers)      |
| Preencher o assistente               | [Iniciar uma execução](/docs/guides/submit)           |
| Ler seus resultados                  | [Resultados](/docs/guides/results)                    |
| Entender o custo                     | [Cobrança](/docs/concepts/billing)                    |
| Usar o Skynet a partir de um agente  | [MCP](/docs/mcp)                                      |
| Chamar o Skynet a partir de código   | [API](/docs/api)                                      |
| Consultar os limites                 | [Limites](/docs/limits)                               |

> [!TIP]
> Comece com um conjunto de dados pequeno e as configurações padrão. Torne o feedback do avaliador específico. Um bom feedback dá ao otimizador mais material de trabalho do que a pontuação sozinha.
