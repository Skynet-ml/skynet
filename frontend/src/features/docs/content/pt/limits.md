O Skynet define limites para execuções, arquivos, armazenamento e taxas de requisição. Os limites mantêm a plataforma estável e protegem o seu saldo. Esta página lista os valores padrão. Um administrador pode alterar alguns limites de uma conta.

> [!NOTE]
> Os valores desta página são os padrões atuais. Quando você atinge um limite, o app ou a API informa qual limite bloqueou a requisição.

## Execuções

| Limite                          | Free | Pro   |
| ------------------------------- | ---- | ----- |
| Execuções ativas ao mesmo tempo | 5    | 20    |
| Execuções salvas                | 100  | 1.000 |

Uma execução ativa é uma execução com o status `pending`, `validating`, `running` ou `paused`. Quando você tem o número máximo de execuções ativas, o Skynet recusa novas execuções até que uma delas termine.

O limite de execuções salvas conta todas as suas execuções. Exclua as execuções de que você não precisa para abrir espaço para novas.

Se a renovação do Pro falhar, a conta mantém os limites do Pro por 7 dias. Depois disso, ela usa os limites do Free até o pagamento ser concluído.

| Limite                                            | Valor                                                                 |
| ------------------------------------------------- | --------------------------------------------------------------------- |
| Cópias em uma requisição de clonagem pela API     | 1 a 5                                                                 |
| Tentativas de recuperação automática por execução | 3                                                                     |
| Casos de amostra na verificação do contrato       | 3                                                                     |
| Eventos de progresso armazenados por execução     | 5.000. O Skynet remove primeiro os eventos mais antigos.              |
| Duração de uma sandbox                            | 45 minutos. Uma execução que precisa de mais tempo abre uma nova sandbox. |

Leia [Execuções](/docs/concepts/runs) para saber como funcionam as tentativas de recuperação.

## Armazenamento

| Limite                 | Free    | Pro    |
| ---------------------- | ------- | ------ |
| Armazenamento total    | 500 MiB | 20 GiB |

O armazenamento total inclui as suas execuções, conjuntos de dados, logs, conversas com o agente, uploads preparados e dados de pesquisa. O Skynet recusa uma gravação que ultrapassaria o seu limite de armazenamento.

## Arquivos e conjuntos de dados

| Limite                                                 | Valor                                     |
| ------------------------------------------------------ | ----------------------------------------- |
| Arquivo de conjunto de dados salvo na sua biblioteca   | 50 MiB por arquivo, após a compressão     |
| Linhas de um conector que importa registros            | 20.000                                    |
| Arquivo anexado a uma conversa com o agente            | 20 MiB                                    |
| Texto lido de um anexo da conversa                     | 50.000 caracteres                         |
| Clipe de áudio para ditado                             | 25 MB                                     |
| Arquivo compactado de repositório do GitHub            | 200 MiB, compactado                       |
| Arquivo do repositório aberto na página da execução    | 1 MiB                                     |

Os conectores que importam registros incluem tabelas de banco de dados, traces de LLM e bancos de dados do Notion. Leia [Preparar um conjunto de dados](/docs/guides/datasets).

### Arquivos do workspace no assistente

| Limite                          | Valor                                                                   |
| ------------------------------- | ----------------------------------------------------------------------- |
| Arquivos extras no workspace    | 20                                                                      |
| Tamanho do nome de um arquivo   | 120 caracteres                                                          |
| Profundidade de pastas          | 3 pastas                                                                |
| Tipos de arquivo                | `.py`, `.txt`, `.md`, `.json`, `.yaml`, `.csv`, `.toml`, `.ini`, `.cfg` |

`run.py` e `metric.py` são os arquivos de entrada de uma execução. Você não pode excluí-los, renomeá-los nem movê-los.

## Taxas de requisição

| Limite                                                               | Valor                                          |
| -------------------------------------------------------------------- | ---------------------------------------------- |
| Envios de execuções                                                  | 30 por conta em um minuto móvel                |
| Clipes de ditado                                                     | 120 por conta em uma hora                      |
| Arquivos lidos para conversas com o agente                           | 300 por conta em uma hora                      |
| Requisições de conta (cadastro, redefinição de senha, verificação de e-mail) | 20 por endereço de e-mail em uma hora móvel |
| Turnos do assistente do formulário                                   | 50 por conta em um dia UTC                     |
| Tokens de pesquisa e indexação                                       | 2.000.000 por conta em um mês                  |

Quando você ultrapassa um limite de taxa, a API retorna o status HTTP `429`. Aguarde e envie a requisição de novo.

Quando uma conta ultrapassa os seus tokens mensais de pesquisa e indexação, o Skynet para de indexar novos itens dessa conta. A pesquisa então funciona como se a pesquisa semântica não estivesse disponível.

## Dinheiro

| Limite                                                     | Valor                                                                          |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Limite de gastos padrão de uma nova execução               | US$ 5, ou o valor necessário para começar, se for maior                        |
| Margem extra no valor necessário para começar              | US$ 1, ou o custo da chamada de modelo individual mais cara, se for maior      |
| Recarga personalizada                                      | US$ 5 a US$ 1.000                                                              |
| Saldo necessário para iniciar um turno interativo de modelo | US$ 0,05                                                                      |

Leia [Orçamentos e cobrança](/docs/concepts/billing) para ver as taxas e as reservas.

## Páginas relacionadas

- [Execuções](/docs/concepts/runs)
- [Orçamentos e cobrança](/docs/concepts/billing)
- [Perguntas frequentes](/docs/faq)
