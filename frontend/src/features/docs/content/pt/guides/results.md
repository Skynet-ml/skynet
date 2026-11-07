A página da execução mostra o progresso e os resultados de uma execução. Este guia explica as abas da página da execução, as pontuações e como exportar ou servir a melhor versão.

## Abas da página da execução

A página da execução mostra apenas as abas que se aplicam à execução. Algumas abas dependem do status e do seu nível de experiência.

| Aba                    | Quando aparece                                             | O que contém                                                              |
| ---------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| **Visão geral**        | Sempre                                                     | Progresso, pontuações e o histórico de pontuações                         |
| **Utilização**         | Uma execução bem-sucedida cujo repositório tem um arquivo `serve.py` | Um formulário para chamar a melhor versão e código de integração |
| **Artefacto**          | Uma execução com resultado                                 | As versões do seu repositório, incluindo a melhor versão                  |
| **Dados**              | Uma execução concluída com casos                           | Os dados divididos em conjuntos de treino, validação e teste              |
| **Código**             | Quando a execução tem código para mostrar                  | O avaliador                                                               |
| **Recompensa**         | Uma execução cujo avaliador declara partes de recompensa   | As partes da recompensa e os pesos delas                                  |
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

Quando o Skynet criou o repositório para a execução, ele envia cada candidato para o seu próprio branch, `skynet/candidate-<version id>`. O branch `skynet/best` sempre aponta para a melhor versão. Ele avança quando aparece uma versão melhor.

## Servir a melhor versão

Você pode chamar a melhor versão a partir do seu próprio código. O repositório deve ter um arquivo `serve.py` na raiz que define `serve(input)`:

```python tab="serve.py"
def serve(input):
    return {"label": classify(input["message"])}
```

- `input` é o dict de campos de entrada da requisição.
- O valor de retorno deve ser serializável em JSON. Um dict vira `outputs`. Qualquer outro valor vira `{"output": value}`.
- `serve.py` pode chamar um modelo com `llm` do módulo auxiliar `skynet`. Ele não recebe os segredos da execução.

O Skynet executa a melhor versão na mesma sandbox protegida da execução. A aba **Utilização** tem duas partes:

- Um formulário. Preencha os campos de entrada e selecione **Executar** para obter um resultado.
- **Código de integração** em cURL, Python, JavaScript e Go.

Para chamar a melhor versão, crie um token de API em **Definições → API**. Depois envie uma requisição. O cabeçalho `Idempotency-Key` é obrigatório:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5}'
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
    json={"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5},
)
print(response.json()["outputs"])
```

Substitua `message` pelos campos de entrada da sua execução. O **Código de integração** da aba **Utilização** preenche a URL real e os nomes dos campos para você.

A resposta contém `outputs`, os `input_fields`, os `output_fields`, o `model_used`, `cents_charged` e `budget`.

> [!TIP]
> Cada chamada é cobrada separadamente, do saldo de quem faz a chamada. `max_cost_cents` define o máximo que esta única chamada pode custar, em centavos. Se você enviar a mesma `Idempotency-Key` de novo, o Skynet não cobra a chamada outra vez.

Leia [API](/docs/api) para ver a autenticação e os outros endpoints.

## Compartilhar os resultados

Você pode compartilhar uma execução com outras pessoas ou listá-la na galeria pública Explorar. Leia [Compartilhar e publicar](/docs/guides/sharing).
