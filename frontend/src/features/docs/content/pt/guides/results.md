A página da execução mostra o progresso e os resultados de uma execução. Este guia explica as abas da página da execução, as pontuações e como exportar a melhor versão.

## Abas da página da execução

A página da execução mostra apenas as abas que se aplicam à execução. Algumas abas dependem do status e do seu nível de experiência.

| Aba                    | Quando aparece                                             | O que contém                                                              |
| ---------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| **Visão geral**        | Sempre                                                     | Progresso, pontuações e o histórico de pontuações                         |
| **Artefacto**          | Uma execução com resultado                                 | As versões do seu repositório, incluindo a melhor versão                  |
| **Dados**              | Uma execução concluída com casos                           | Os dados divididos em conjuntos de treino, validação e teste              |
| **Código**             | Quando a execução tem código para mostrar                  | O avaliador                                                               |
| **Registos**           | A maioria das execuções                                    | O log da execução                                                         |
| **Utilização e custo** | Quando você tem sessão iniciada                            | O que a execução gastou                                                   |
| **Execução agendada**  | Uma execução bem-sucedida                                  | Um agendamento para executar a otimização de novo a partir da melhor versão |
| **Configuração**       | Sempre                                                     | A configuração da execução                                                |

> [!NOTE]
> No nível **Guiado**, a página da execução oculta as abas **Dados**, **Código**, **Registos** e **Configuração**. Um link direto para uma dessas abas ainda a abre. Altere o nível em **Definições → Conta**.

## Pontuações

O Skynet seleciona as versões com base nos casos de validação. Ele informa a pontuação final com base nos casos de teste. Os motores nunca veem os casos de teste. Leia [Conjuntos de dados e divisões](/docs/concepts/datasets).

A aba **Visão geral** mostra:

- **Melhoria**: a variação da versão inicial para a melhor versão.
- **Progressão da pontuação**: a pontuação ao longo da execução.
- **Detalhe da pontuação**: cada pontuação, com as colunas **Linha de base**, **Otimizado** e **Variação**.

Enquanto a execução está ativa, a visão geral também mostra **Decorrido**, **Restante** e **Taxa**.

A visão geral também mostra **Pontuações nomeadas** quando o seu avaliador informa pontuações nomeadas.

### Quando a melhor versão é a versão inicial

Às vezes nenhum candidato supera a versão inicial. Nesse caso, o Skynet retorna a versão inicial. A aba **Artefacto** informa que uma proteção contra regressão foi aplicada.

Se a execução parou no limite do orçamento, a página da execução informa qual versão o Skynet salvou. Leia [Execuções](/docs/concepts/runs).

## Exportar a melhor versão

A aba **Artefacto** lista as **Versões** do seu repositório. Para cada versão, você pode:

- Copiar a versão.
- Baixar a versão como arquivo.
- Baixar a versão como um zip do repositório inteiro.

Você pode comparar uma versão com outra. A aba também mostra o **Feedback do avaliador** de uma versão.

### Branches em um novo repositório

Quando o Skynet criou o repositório para a execução no GitHub, ele envia cada candidato para o seu próprio branch, `skynet/candidate-<version id>`. O branch `skynet/best` sempre aponta para a melhor versão. Ele avança quando aparece uma versão melhor.

## Compartilhar os resultados

Você pode compartilhar uma execução com outras pessoas ou listá-la na galeria pública Explorar. Leia [Compartilhar e publicar](/docs/guides/sharing).
