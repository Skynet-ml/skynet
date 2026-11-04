Um motor é o método de busca que propõe novas versões do seu artefato. O Skynet tem seis motores nativos para repositórios, três otimizadores DSPy para programas DSPy e o **Auto**. Esta página explica o que cada um faz, quando escolhê-lo e quais configurações ele tem.

Leia primeiro [Como a otimização funciona](/docs/concepts/optimization) se você não souber quais motores servem para o seu artefato.

## Comparação rápida

| Motor                 | Tipos de artefato     | Configurações do agente de programação | Configuração de máx. de iterações |
| --------------------- | --------------------- | -------------------------------------- | --------------------------------- |
| Auto | Programa DSPy | Não | Não |
| GEPA | Repositório | Não | Não |
| Best-of-N | Repositório | Não | Não |
| AutoResearch | Repositório | Sim | Não |
| Meta-Harness | Repositório | Sim | Sim |
| AutoSaddler | Repositório | Sim | Sim |
| ShinkaEvolve | Repositório | Não. Tem a sua própria configuração de editor. | Sim |
| DSPy GEPA             | Programa DSPy         | Não                                    | Não                               |
| DSPy MIPROv2          | Programa DSPy         | Não                                    | Não                               |
| DSPy BootstrapFewShot | Programa DSPy         | Não                                    | Não                               |

O assistente marca o GEPA, o DSPy GEPA e o Auto como otimizadores que usam o texto de feedback da sua métrica.

## Auto

O Auto é o padrão. O que ele executa depende do tipo de artefato:

- **Programa DSPy**: o Auto executa o DSPy GEPA.
- **Repositório**: O Auto não está disponível. Escolha um motor nativo.

**Escolha-o quando** você não sabe qual motor serve. Este é o melhor ponto de partida.

## GEPA

"Evolução reflexiva com uma fronteira de Pareto de versões." O modelo de reflexão lê a pontuação e o feedback de cada caso e escreve uma versão melhor. O GEPA mantém uma fronteira de versões em que cada uma é a melhor em alguns casos.

**Escolha-o quando** a sua métrica dá um feedback específico e você quer um único motor forte.

## Best-of-N

"Propostas independentes do modelo de reflexão; mantém a melhor." Cada proposta é independente das outras. O Skynet volta a pontuar as melhores propostas nos casos de validação.

**Escolha-o quando** você quer uma linha de base simples para comparar com outros motores.

## AutoResearch

Um agente de programação faz rodadas de pesquisa. Ele edita, pontua e mantém o que ajuda até o orçamento acabar.

**Escolha-o quando** uma boa mudança exige várias etapas de investigação.

## Meta-Harness

Um agente de programação propõe um lote de candidatos a cada iteração. Ele usa o histórico completo de candidatos e pontuações.

**Escolha-o quando** você quer um agente que aprenda com o histórico de todas as tentativas anteriores. Use **Candidatos por iteração** para definir o tamanho do lote.

## AutoSaddler

Um agente de programação lê o feedback da sua métrica e corrige a versão. O Skynet mantém apenas as correções que uma nova execução do avaliador confirma.

**Escolha-o quando** o feedback da sua métrica aponta para defeitos claros e localizados.

## ShinkaEvolve

"Evolui muitas versões em ilhas paralelas e aprende quais modelos as melhoram mais." É uma busca evolutiva sobre uma população de candidatos, com mutações e cruzamentos escritos por um modelo.

**Escolha-o quando** você quer uma exploração ampla e tem um orçamento maior.

## Otimizadores DSPy

| Otimizador            | O que faz                                                                                                                         |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| DSPy GEPA             | Lê o feedback da métrica e evolui as instruções de cada preditor.                                                                 |
| DSPy MIPROv2          | Propõe instruções e demonstrações few-shot em conjunto e busca as combinações delas com otimização bayesiana.                     |
| DSPy BootstrapFewShot | Coleta exemplos resolvidos que o programa acerta e os adiciona como demonstrações few-shot. Não usa um modelo de reflexão.         |

O DSPy MIPROv2 e o DSPy BootstrapFewShot precisam de pares (casos com saídas esperadas).

## Configurações

### Orçamento

| Configuração                   | Padrão     | Intervalo                                                    |
| ------------------------------ | ---------- | ------------------------------------------------------------ |
| Máx. de execuções do avaliador | 200        | 1 a 100000                              |
| Parar na pontuação             | Desativada | 0 ou mais                                                    |
| Máx. de iterações              | Desativada | 1 a 1000. Apenas Meta-Harness, AutoSaddler e ShinkaEvolve.   |

Uma execução do avaliador é uma chamada da sua métrica em um caso. A nova pontuação final da versão inicial e do vencedor não conta para **Máx. de execuções do avaliador**.

### Configurações do agente de programação

Estas configurações valem para AutoResearch, Meta-Harness e AutoSaddler.

| Configuração                                   | Padrão | Intervalo                                                |
| ---------------------------------------------- | ------ | -------------------------------------------------------- |
| Agente de programação                          | Codex  | Pi, Codex, OpenCode, Prime Agent, Comando personalizado  |
| Candidatos por iteração                        | 3      | 1 a 8. |
| Máx. de chamadas de ferramentas por candidato  | 100    | 1 a 1000                                                 |

### Configurações do ShinkaEvolve

| Configuração                                       | Padrão                                                              |
| -------------------------------------------------- | ------------------------------------------------------------------- |
| Como as novas versões são escritas                 | Uma única chamada ao modelo. A outra opção é "Agente de código (Pi)". |
| Ilhas                                              | 2 (1 a 8)                                                           |
| Intervalo de migração                              | 10                                                                  |
| Seleção do pai                                     | Ponderada. Outras opções: Lei de potência, Pesquisa em feixe.       |
| Edições diff / Reescritas completas / Cruzamentos  | 60% / 30% / 10%. A soma deve ser 100%.                              |
| Tamanho do arquivo                                 | 40                                                                  |
| Avaliações paralelas                               | 2                                                                   |
| Propostas paralelas                                | 2                                                                   |

## Páginas relacionadas

- [Como a otimização funciona](/docs/concepts/optimization)
- [Métricas e avaliadores](/docs/concepts/scorers)
- [Execuções](/docs/concepts/runs)
- [Cobrança](/docs/concepts/billing)
