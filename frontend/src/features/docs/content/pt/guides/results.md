A página da execução mostra o progresso e os resultados de uma execução. Este guia explica as abas da página da execução, as pontuações e como exportar ou servir a melhor versão.

## Abas da página da execução

A página da execução mostra apenas as abas que se aplicam à execução. Algumas abas dependem do caminho, do status e do seu nível de experiência.

| Aba                      | Quando aparece                                                         | O que contém                                                                          |
| ------------------------ | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| **Visão geral**          | Sempre                                                                 | Progresso, pontuações e o histórico de pontuações                                     |
| **Utilização**           | Uma execução bem-sucedida com um programa que o Skynet pode servir     | Um formulário para executar o programa otimizado e código de integração               |
| **Artefacto** | Uma execução de repositório com resultado | As versões do seu artefato, incluindo a melhor versão |
| **Dados**                | Uma execução de programa DSPy concluída                                | Os dados divididos em conjuntos de treino, validação e teste, e a avaliação do conjunto de teste |
| **Código**               | Quando a execução tem código para mostrar                              | O código de execução e a métrica                                                      |
| **Recompensa** | Uma execução de repositório cuja métrica declara partes de recompensa | As partes da recompensa e os pesos delas |
| **Artefacto** | Uma execução de programa DSPy concluída | A saída otimizada e **Exportar resultados** |
| **Registos**             | A maioria das execuções                                                | O log da execução                                                                     |
| **Utilização e custo**   | Quando você tem sessão iniciada                                        | O que a execução gastou                                                               |
| **Execução agendada** | Uma execução bem-sucedida de programa DSPy | Um agendamento para executar a otimização de novo |
| **Configuração**         | Sempre                                                                 | A configuração da execução                                                            |

> [!NOTE]
> No nível **Guiado**, a página da execução oculta as abas **Dados**, **Código**, **Registos** e **Configuração**. Um link direto para uma dessas abas ainda a abre. Altere o nível em **Definições → Conta**.

## Pontuações

O Skynet seleciona as versões com base nos casos de validação. Ele informa a pontuação final com base nos casos de teste. Os motores nunca veem os casos de teste. Leia [Conjuntos de dados e divisões](/docs/concepts/datasets).

A aba **Visão geral** mostra:

- **Melhoria**: a variação da versão inicial para a melhor versão.
- **Progressão da pontuação**: a pontuação ao longo da execução.
- **Detalhe da pontuação**: cada métrica, com as colunas **Linha de base**, **Otimizado** e **Variação**.

Enquanto a execução está ativa, a visão geral também mostra **Decorrido**, **Restante** e **Taxa**.

Em uma execução de repositório, a visão geral também mostra **Pontuações nomeadas** quando sua métrica informa pontuações nomeadas.

Para uma execução de programa DSPy, a aba **Dados** tem uma **Avaliação do conjunto de teste**. Ela mostra a pontuação de linha de base e a pontuação otimizada de cada caso de teste.

### Quando a melhor versão é a versão inicial

Às vezes nenhum candidato supera a versão inicial. Nesse caso, o Skynet retorna a versão inicial. A aba **Artefacto** informa que uma proteção contra regressão foi aplicada.

Se a execução parou no limite do orçamento, a página da execução informa qual versão o Skynet salvou. Leia [Execuções](/docs/concepts/runs).

## Exportar a melhor versão

### Execuções de repositório

A aba **Artefacto** lista as **Versões** do seu artefato. Para cada versão, você pode:

- Copiar a versão.
- Baixar a versão como arquivo.
- Em uma execução de repositório, baixar a versão como um zip do repositório inteiro.

Você pode comparar uma versão com outra. A aba também mostra o **Feedback do avaliador** de uma versão.

### Execuções de programa DSPy

A aba **Artefacto** tem um painel **Exportar resultados**. Abra o menu **Transferir**. Ele mostra os formatos que a execução tem:

| Opção                  | Formato                                                  |
| ---------------------- | -------------------------------------------------------- |
| Programa DSPy          | `.zip`, uma exportação autossuficiente que você pode executar |
| Programa otimizado     | `.pkl`                                                   |
| Prompt                 | `.json`                                                  |
| Código otimizado       | `.py`                                                    |
| Registos               | `.csv`                                                   |

## Servir a melhor versão

Você pode chamar um programa DSPy otimizado a partir do seu próprio código. A aba **Utilização** tem duas partes:

- Um formulário. Preencha os campos de entrada e selecione **Executar** para obter uma previsão.
- **Código de integração** em cURL, Python, JavaScript, Go e DSPy.

O programa sempre roda com as configurações de modelo com as quais foi otimizado. Você deve informar todos os campos de entrada. O Skynet ignora campos extras.

Para chamar o programa, crie um token de API em **Definições → API**. Depois envie uma requisição:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"question": "What is DSPy?"}}'
```

```python tab="Python"
import os
import uuid
import requests

response = requests.post(
    f"{os.environ['API_URL']}/serve/<optimization_id>",
    headers={
        "Authorization": f"Bearer {os.environ['APP_API_TOKEN']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={"inputs": {"question": "What is DSPy?"}},
)
print(response.json()["outputs"])
```

Substitua `question` pelos campos de entrada do seu programa. O **Código de integração** da aba **Utilização** preenche a URL real e os nomes dos campos para você.

A resposta contém `outputs`, os `input_fields`, os `output_fields` e o `model_used`.

> [!TIP]
> O corpo da requisição também aceita `max_cost_cents`. Ele define o máximo que esta única chamada pode custar, em centavos. Algumas execuções exigem esse campo.

Para receber saídas parciais à medida que chegam, envie a mesma requisição para `/serve/<optimization_id>/stream`. A resposta é um fluxo de server-sent events.

Leia [API](/docs/api) para ver a autenticação e os outros endpoints.

## Compartilhar os resultados

Você pode compartilhar uma execução com outras pessoas ou listá-la na galeria pública Explorar. Leia [Compartilhar e publicar](/docs/guides/sharing).
