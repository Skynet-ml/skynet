A métrica diz ao Skynet quão boa é uma saída. Ela é uma função Python em `metric.py`. Todos os motores chamam a mesma função. Esta página apresenta o contrato, o valor de retorno e exemplos.

## O contrato

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argumento  | O que contém                                                     |
| ---------- | ---------------------------------------------------------------- |
| `input`    | Um dict com os campos de entrada do caso                         |
| `output`   | O que a sua `run(artifact, input)` retornou para o caso          |
| `expected` | O valor esperado do caso, ou `None` quando o caso não tem um     |

Se a sua função receber apenas dois argumentos, o Skynet chama `score(input, output)`.

Para um programa DSPy, o Skynet adapta a chamada de métrica do DSPy a esta mesma função. Você escreve uma única métrica para todos os tipos de artefato. Leia [Como a otimização funciona](/docs/concepts/optimization).

## O valor de retorno

`score` deve retornar um par (uma tupla ou uma lista de dois itens):

1. **A pontuação.** Um número. O otimizador tenta aumentá-lo.
2. **As informações adicionais.** Um dict. Ele deve ter uma chave `"feedback"` com um valor do tipo string.

As informações adicionais também podem ter uma chave `"scores"`. Ela mapeia nomes para números, por exemplo `{"format": 1.0, "accuracy": 0.5}`.

Se o valor de retorno tiver outro formato, a verificação do contrato falha em **Valor devolvido pela métrica**.

| Valor de retorno                                       | Válido                                         |
| ------------------------------------------------------ | ---------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Sim                                            |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Sim                                            |
| `0.8`                                                  | Não. Não há informações adicionais.            |
| `(0.8, "Close.")`                                      | Não. As informações adicionais não são um dict. |
| `(0.8, {})`                                            | Não. Não há string de feedback.                |
| `("0.8", {"feedback": "OK"})`                          | Não. A pontuação é uma string.                 |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | Não. Uma pontuação nomeada não é um número.    |

## Escrever um feedback útil

O GEPA, o DSPy GEPA e o Auto leem o texto de feedback para escrever a próxima versão. Diga o que está errado e como é uma saída correta.

| Feedback fraco | Feedback útil                                                                       |
| -------------- | ----------------------------------------------------------------------------------- |
| "Errado."      | "Esperado 'billing', obtido 'other'. A mensagem menciona uma cobrança."             |
| "Formato ruim." | "A saída não é um JSON válido. Há uma vírgula sobrando depois do último campo."    |

> [!NOTE]
> O otimizador nunca vê o código da sua métrica. Com um otimizador DSPy, o modelo de reflexão vê apenas o texto de feedback. Os seus valores esperados ficam com a métrica.

## Exemplos

### Correspondência exata

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` é um dict indexado pelo nome da sua coluna de Saída. Leia [Conjuntos de dados e divisões](/docs/concepts/datasets).

### Várias verificações

```python tab="metric.py"
import json


def score(input, output, expected=None):
    try:
        data = json.loads(output)
    except ValueError as exc:
        return 0.0, {"feedback": f"Output is not valid JSON: {exc}", "scores": {"format": 0.0, "fields": 0.0}}
    want = expected["fields"]
    found = [key for key in want if key in data]
    fields = len(found) / len(want) if want else 1.0
    missing = [key for key in want if key not in data]
    feedback = "All fields present." if not missing else f"Missing fields: {', '.join(missing)}."
    return 0.5 + 0.5 * fields, {"feedback": feedback, "scores": {"format": 1.0, "fields": fields}}
```

### Modelo como juiz

A métrica pode chamar um modelo. Importe `llm` do módulo auxiliar `skynet`. Uma chamada a partir da métrica vai para o modelo da métrica.

```python tab="metric.py"
from skynet import llm


def score(input, output, expected=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {input['question']}\nAnswer: {output}",
        model="openai/gpt-4.1-mini",
        temperature=0,
        max_tokens=512,
        reasoning_effort="low",
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

A métrica define seu modelo na própria chamada a `llm()`. Os quatro argumentos mostrados são opcionais. Sem `model=`, a métrica usa o modelo de reflexão.

## Precisão, velocidade e custo

Uma métrica pode combinar a precisão com o tempo de execução medido e o custo do modelo. Defina `REWARD_SPEC` no nível do módulo e retorne o valor de `reward`.

```python tab="metric.py"
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.7, "speed": 0.2, "cost": 0.1},
    "accuracy": {},
    "speed": {"reference": 2000},
    "cost": {"reference": 0.01},
}


def score(input, output, expected=None):
    ok = output == expected["label"]
    return reward(REWARD_SPEC, 1.0 if ok else 0.0, "Correct." if ok else "Wrong label.")
```

- `weights` deve ter `accuracy`, `speed` e `cost`. Cada peso é 0 ou mais, e nem todos podem ser 0. O Skynet os reescala para que somem 1.
- `speed.reference` é em milissegundos. `cost.reference` é em dólares americanos. Os dois devem ser positivos.
- Sem `accuracy.reference`, a precisão é uma pontuação de 0 a 1. Com ele, a precisão é um erro, e quanto menor, melhor.

O próprio Skynet mede o tempo e o custo. O código de execução não pode alterá-los. Se o Skynet não conseguir medir o tempo ou o custo, essa parte recebe 0 e o feedback informa isso.

A métrica também pode ler as medições diretamente com `observation()` e `service_cost()` de `skynet`.

## Limites

| Item                           | Valor                                  |
| ------------------------------ | -------------------------------------- |
| Tempo limite da métrica        | 60 segundos por padrão, no máximo 600  |
| Amostras da verificação do contrato | 3 casos                           |

## Páginas relacionadas

- [Conjuntos de dados e divisões](/docs/concepts/datasets)
- [Motores](/docs/concepts/engines)
- [Iniciar uma execução](/docs/guides/submit)
