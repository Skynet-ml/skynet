O Skynet melhora um artefato que você já tem. Você fornece uma versão inicial, um conjunto de casos e uma métrica. O Skynet propõe novas versões, executa cada uma nos seus casos, pontua a saída com a sua métrica e mantém a melhor versão.

## O que o Skynet otimiza

O Skynet trabalha com dois tipos de artefato:

| Tipo de artefato | O que muda                                                     |
| ---------------- | -------------------------------------------------------------- |
| Repositório | Os arquivos nos caminhos que você permite alterar |
| Programa DSPy    | As instruções dos preditores em um `dspy.Module`               |

Toda execução usa o mesmo contrato. O seu código de execução define `run(artifact, input)`. A sua métrica define `score(input, output, expected=None)`. A métrica retorna uma pontuação e um feedback escrito. O otimizador lê o feedback para melhorar a próxima versão.

> [!NOTE]
> O seu código de execução e a sua métrica rodam em uma sandbox, em processos separados. O código de execução não vê o código da métrica nem os valores esperados.

## Um produto, vários motores

O Skynet é o Optimize Anything. O tipo de artefato define quais motores podem trabalhar nele:

- Repositórios usam os motores nativos: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler e ShinkaEvolve.
- Artefatos de programa DSPy usam os otimizadores DSPy: DSPy GEPA, DSPy MIPROv2 e DSPy BootstrapFewShot.

Em um programa DSPy, o **Auto** usa o DSPy GEPA. Em um repositório, você escolhe um motor nativo. Leia [Como a otimização funciona](/docs/concepts/optimization) e [Motores](/docs/concepts/engines).

## Como funciona uma execução

1. Você descreve a tarefa e define a versão inicial.
2. Você fornece os casos. O Skynet os divide em conjuntos de treino, validação e teste.
3. Você seleciona um otimizador, os modelos e um orçamento.
4. Você escreve o código de execução e a métrica. Uma verificação do contrato os executa em casos de amostra.
5. O Skynet executa a otimização e mostra os resultados.

O Skynet seleciona as versões com base nos casos de validação. Ele informa a pontuação final com base nos casos de teste. Os motores nunca veem os casos de teste.

## Por onde começar

| Se você quer                         | Leia                                                  |
| ------------------------------------ | ----------------------------------------------------- |
| Iniciar sua primeira execução        | [Início rápido](/docs/quickstart)                     |
| Entender como a otimização funciona  | [Como a otimização funciona](/docs/concepts/optimization) |
| Escolher um motor                    | [Motores](/docs/concepts/engines)                     |
| Preparar seus dados                  | [Conjuntos de dados e divisões](/docs/concepts/datasets) |
| Escrever uma métrica                 | [Métricas e avaliadores](/docs/concepts/scorers)      |
| Preencher o assistente               | [Iniciar uma execução](/docs/guides/submit)           |
| Ler seus resultados                  | [Resultados](/docs/guides/results)                    |
| Entender o custo                     | [Cobrança](/docs/concepts/billing)                    |
| Usar o Skynet a partir de um agente  | [MCP](/docs/mcp)                                      |
| Chamar o Skynet a partir de código   | [API](/docs/api)                                      |
| Consultar os limites                 | [Limites](/docs/limits)                               |

> [!TIP]
> Comece com um conjunto de dados pequeno e as configurações padrão. Torne o feedback da métrica específico. Um bom feedback dá ao otimizador mais material de trabalho do que a pontuação sozinha.
