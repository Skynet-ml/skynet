La métrica le dice a Skynet lo buena que es una salida. Es una función de Python en `metric.py`. Todos los motores llaman a la misma función. Esta página explica el contrato, el valor devuelto y algunos ejemplos.

## El contrato

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argumento  | Qué contiene                                                          |
| ---------- | --------------------------------------------------------------------- |
| `input`    | Un dict con los campos de entrada del caso                            |
| `output`   | Lo que devolvió tu `run(artifact, input)` para el caso                |
| `expected` | El valor esperado del caso, o `None` cuando el caso no tiene ninguno  |

Si tu función solo recibe dos argumentos, Skynet llama a `score(input, output)`.

Para un programa DSPy, Skynet adapta la llamada a la métrica de DSPy a esta misma función. Escribes una sola métrica para todos los tipos de artefacto. Lee [Cómo funciona la optimización](/docs/concepts/optimization).

## El valor devuelto

`score` debe devolver un par (una tupla o una lista de dos elementos):

1. **La puntuación.** Un número. El optimizador intenta que sea más alto.
2. **La información adicional.** Un dict. Debe tener una clave `"feedback"` con un valor de tipo cadena.

La información adicional también puede tener una clave `"scores"`. Asocia nombres a números, por ejemplo `{"format": 1.0, "accuracy": 0.5}`.

Si el valor devuelto tiene otra forma, la comprobación del contrato falla en **Valor que devuelve la métrica**.

| Valor devuelto                                         | Válido                                        |
| ------------------------------------------------------ | --------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Sí                                            |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Sí                                            |
| `0.8`                                                  | No. No hay información adicional.             |
| `(0.8, "Close.")`                                      | No. La información adicional no es un dict.   |
| `(0.8, {})`                                            | No. No hay cadena de comentarios.             |
| `("0.8", {"feedback": "OK"})`                          | No. La puntuación es una cadena.              |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | No. Una puntuación con nombre no es un número. |

## Escribe comentarios útiles

GEPA, DSPy GEPA y Auto leen el texto de los comentarios para escribir la siguiente versión. Di qué está mal y cómo es una salida correcta.

| Comentario pobre    | Comentario útil                                                                       |
| ------------------- | ------------------------------------------------------------------------------------- |
| «Incorrecto.»       | «Se esperaba 'billing' y se obtuvo 'other'. El mensaje menciona un cargo.»            |
| «Formato erróneo.»  | «La salida no es un JSON válido. Tiene una coma sobrante después del último campo.»   |

> [!NOTE]
> El optimizador nunca ve el código fuente de tu métrica. Con un optimizador de DSPy, el modelo de reflexión solo ve el texto de los comentarios. Tus valores esperados se quedan con la métrica.

## Ejemplos

### Coincidencia exacta

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` es un dict cuya clave es el nombre de tu columna de salida. Lee [Conjuntos de datos y divisiones](/docs/concepts/datasets).

### Varias comprobaciones

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

### Modelo como juez

La métrica puede llamar a un modelo. Importa `llm` desde el módulo auxiliar `skynet`. Una llamada desde la métrica va al modelo de la métrica.

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

La métrica define su modelo en su propia llamada a `llm()`. Los cuatro argumentos mostrados son opcionales. Sin `model=`, la métrica usa el modelo de reflexión.

## Precisión, velocidad y coste

Una métrica puede combinar la precisión con el tiempo de ejecución medido y el coste del modelo. Define `REWARD_SPEC` a nivel de módulo y devuelve el valor de `reward`.

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

- `weights` debe tener `accuracy`, `speed` y `cost`. Cada peso es 0 o más, y no pueden ser todos 0. Skynet los escala para que sumen 1.
- `speed.reference` está en milisegundos. `cost.reference` está en dólares estadounidenses. Ambos deben ser positivos.
- Sin `accuracy.reference`, la precisión es una puntuación de 0 a 1. Con él, la precisión es un error, y cuanto más bajo, mejor.

Skynet mide el tiempo y el coste por sí mismo. El código de ejecución no puede cambiarlos. Si Skynet no puede medir el tiempo o el coste, esa parte puntúa 0 y los comentarios lo indican.

La métrica también puede leer las mediciones directamente con `observation()` y `service_cost()` de `skynet`.

## Límites

| Elemento                                    | Valor                                         |
| ------------------------------------------- | --------------------------------------------- |
| Tiempo límite de la métrica                 | 60 segundos por defecto, como máximo 600      |
| Muestras de la comprobación del contrato    | 3 casos                                       |

## Páginas relacionadas

- [Conjuntos de datos y divisiones](/docs/concepts/datasets)
- [Motores](/docs/concepts/engines)
- [Iniciar una ejecución con el asistente](/docs/guides/submit)
