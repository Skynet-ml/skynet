A API HTTP do Skynet permite que scripts e ferramentas façam o que o aplicativo web faz: iniciar execuções, acompanhá-las e ler os resultados. Esta página mostra como assinar as requisições, onde está a referência completa e um exemplo completo.

> [!TIP]
> Você usa um agente de IA como Claude Code, Codex ou Cursor? Conecte-o pelo [MCP](/docs/mcp) em vez disso. O agente então inicia e acompanha execuções por você, com um limite de gastos para cada execução.

## URL base

Envie todas as requisições para:

```text
https://api.skynetml.com
```

## Autenticação

A API usa tokens de API.

1. Abra **Definições** no Skynet e depois a aba **API**.
2. Clique em **Gerar token**.
3. Copie o token imediatamente. O Skynet mostra o token completo uma única vez.

Envie o token no cabeçalho `Authorization` de cada requisição:

```http
Authorization: Bearer skyd_...
```

- Os tokens começam com `skyd_`.
- Cada usuário tem no máximo um token válido. Um novo token invalida o token anterior.
- Clique em **Revogar token** para invalidar um token.

> [!WARNING]
> Trate o token como uma senha. Mantenha-o fora do código-fonte e dos logs. Leia [Segurança](/docs/security) para saber como o Skynet armazena os tokens.

## Referência interativa

A referência completa está em [api.skynetml.com/scalar](https://api.skynetml.com/scalar). Ela lista cada endpoint público, os seus campos e as suas respostas. Você pode enviar requisições de teste a partir dessa página.

A referência lê o documento OpenAPI em `https://api.skynetml.com/openapi.public.json`. Use esse arquivo para gerar um cliente na sua linguagem.

## Endpoints principais

| Método e caminho                                | O que faz                                                      |
| ----------------------------------------------- | -------------------------------------------------------------- |
| `GET /models`                                   | Lista os modelos que você pode usar.                           |
| `GET /optimizers`                               | Lista os otimizadores adequados a uma configuração.            |
| `GET /blackbox/engines`                         | Lista os motores nativos e se cada um está disponível.         |
| `POST /optimize` | Inicia uma execução para um programa DSPy. |
| `POST /blackbox/run`                            | Inicia uma execução em um repositório do GitHub.               |
| `GET /optimizations`                            | Lista as suas execuções.                                       |
| `GET /optimizations/{optimization_id}`          | Retorna uma execução: status, progresso, logs e resultado.     |
| `GET /optimizations/{optimization_id}/artifact` | Retorna a melhor versão que a execução encontrou.              |
| `POST /optimizations/{optimization_id}/cancel`  | Para uma execução.                                             |
| `POST /serve/{optimization_id}`                 | Executa a melhor versão em uma nova entrada.                   |
| `GET /health`                                   | Mostra se o serviço está no ar.                                |

## Iniciar uma execução

`POST /optimize` recebe a mesma configuração que o assistente de envio. O exemplo melhora um programa DSPy que classifica mensagens de suporte em rótulos.

```bash tab="curl"
curl https://api.skynetml.com/optimize \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "task": {
    "objective": "Sort each support message into billing, bug or other.",
    "artifact": {
      "kind": "dspy_program",
      "starting_version": "Label this support message as billing, bug or other."
    }
  },
  "data": {
    "mode": "pairs",
    "cases": [
      { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
      { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
      {
        "input": { "message": "Do you have a Hebrew interface?" },
        "expected": { "label": "other" }
      }
    ]
  },
  "optimizer": { "choice": "auto", "budget": { "max_scorer_runs": 200 } },
  "models": {
    "task_model": { "name": "<model from GET /models>" },
    "reflection_model": { "name": "<model from GET /models>" }
  },
  "workspace": {
    "run_code": "import dspy\n\nprogram = dspy.Predict(\"message -> label\")\n\n\ndef run(artifact, input):\n    return program(message=input[\"message\"]).label\n",
    "metric_code": "def score(input, output, expected=None):\n    ok = str(output).strip().lower() == expected[\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n"
  },
  "max_cost_cents": 500
}
```

Campos importantes:

- `task.artifact.kind` é `dspy_program`.
- `data.mode` é `pairs` (entradas com saídas esperadas) ou `single` (exatamente um caso).
- `optimizer.choice` é `auto` ou o nome de um otimizador retornado por `GET /optimizers`.
- `max_cost_cents` é o limite de gastos desta execução, em centavos de dólar americano.
- `is_private` é `false` por padrão. Defina como `true` para manter a execução privada.

Leia [Métricas e avaliadores](/docs/concepts/scorers) para ver as regras de `run_code` e `metric_code`.

### Respostas

| Status | Significado                                                                                                                                              |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | O Skynet colocou a execução na fila. O corpo contém `optimization_id` e `status`.                                                                        |
| `202`  | A verificação da configuração ainda está em andamento. Aguarde os segundos do cabeçalho `Retry-After`. Depois envie o mesmo corpo com a mesma `Idempotency-Key`. |
| `402`  | O seu saldo é insuficiente.                                                                                                                              |
| `409`  | Você atingiu uma cota, ou a verificação da configuração está desatualizada.                                                                              |
| `422`  | A requisição ou o código do workspace não é compatível com o otimizador.                                                                                 |

> [!NOTE]
> Envie sempre um cabeçalho `Idempotency-Key`. Se você enviar a mesma requisição de novo com a mesma chave, o Skynet retorna a primeira execução. Ele não inicia uma segunda execução.

## Acompanhar uma execução

Consulte a execução até que o status seja final:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

O campo `status` é um destes:

| Status                             | Final |
| ---------------------------------- | ----- |
| `pending`, `validating`, `running` | Não   |
| `paused`                           | Não   |
| `success`                          | Sim   |
| `failed`, `cancelled`, `stopped`   | Sim   |

Consulte a cada 10 a 30 segundos. Uma execução pode levar de minutos a horas.

A [referência da API](https://api.skynetml.com/scalar) lista todos os campos da resposta da execução.

## Usar o resultado

Quando o status for `success`, obtenha a melhor versão:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Para executar a melhor versão em uma nova entrada, envie `POST /serve/{optimization_id}`. Leia [Resultados](/docs/guides/results) para saber o que cada pontuação significa.

## Limites

A API usa as mesmas cotas, limites de taxa e cobrança que o aplicativo web. Leia [Limites](/docs/limits) e [Cobrança](/docs/concepts/billing).
