El evaluador le dice a Skynet lo buena que es una versión de tu repositorio. Es una función de Python en `scorer.py`. Todos los motores llaman a la misma función. Esta página explica el contrato, el valor devuelto y algunos ejemplos.

## El contrato

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argumento   | Qué contiene                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Una carpeta que contiene solo tus archivos de métrica. No contiene ningún otro archivo del repositorio ni código de la versión |
| `case`      | Un dict. `case["input"]` contiene los campos de entrada. `case["expected"]` contiene el valor esperado, o `None` |
| `output`    | Lo que la entrada `[run]` devolvió para el caso |

El archivo `optimize.toml` del repositorio debe tener una sección `[run]` o una etapa `[target.<name>]`. El `entry` de `[run]` ejecuta una versión sobre un caso, en su propia caja de sandbox. Skynet rechaza un archivo sin ninguna de las dos, porque cada versión obtendría la misma puntuación.

- Una entrada de función, por ejemplo `entry = "serve.py:serve"`, recibe `case["input"]` como argumento. `output` es su valor devuelto.
- Una entrada de comando, por ejemplo `entry = ["python", "-m", "pytest", "-q"]`, recibe la entrada como JSON en stdin. `output` es `{"exit_code": ..., "stdout": "..."}`.

El evaluador se ejecuta en una caja de sandbox separada. Esa caja contiene solo tus archivos de métrica. El evaluador no puede importar ni ejecutar el código de la versión. Ve la versión solo a través de `output`, o a través de `call()` a una etapa `[target.<name>]`. Importa `call` con `from skynet import call`. Lee [Cómo funciona la optimización](/docs/concepts/optimization).

## El valor devuelto

`score` debe devolver una puntuación con comentarios. Estas formas son válidas:

1. Un par: un número, y una cadena de comentarios o un dict con una clave `"feedback"`.
2. Un dict con un número en `"score"` y una cadena en `"feedback"`.

Los comentarios deben ser una cadena que no esté vacía.

La información adicional también puede tener una clave `"scores"`. Asocia nombres a puntuaciones con nombre. Cada puntuación con nombre tiene su propia puntuación y sus propios comentarios, por ejemplo `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

La información adicional también puede tener una clave `"cases"` con la misma forma. Cada entrada es el resultado de un caso. Todos los motores usan cada caso como un objetivo propio.

Si el valor devuelto tiene otra forma, la comprobación falla y muestra el motivo.

| Valor devuelto                                                                  | Válido                                         |
| ------------------------------------------------------------------------------- | ---------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | Sí                                             |
| `(0.8, "Close. Missing the date.")`                                             | Sí                                             |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | Sí                                  |
| `0.8`                                                                           | No. No hay comentarios.                        |
| `(0.8, {})`                                                                     | No. No hay cadena de comentarios.              |
| `("0.8", {"feedback": "OK"})`                                                   | No. La puntuación es una cadena.               |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | No. Una puntuación con nombre no es un número. |

## Escribe comentarios útiles

GEPA y Auto leen el texto de los comentarios para escribir la siguiente versión. Di qué está mal y cómo es una salida correcta.

| Comentario pobre    | Comentario útil                                                                       |
| ------------------- | ------------------------------------------------------------------------------------- |
| «Incorrecto.»       | «Se esperaba 'billing' y se obtuvo 'other'. El mensaje menciona un cargo.»            |
| «Formato erróneo.»  | «La salida no es un JSON válido. Tiene una coma sobrante después del último campo.»   |

> [!NOTE]
> El agente de código nunca ve el código fuente de tu evaluador, los valores esperados ni los secretos de la ejecución. Solo ve las puntuaciones y el texto de los comentarios.

## Ejemplos

### Coincidencia exacta

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` es un dict cuya clave es el nombre de tu columna de salida. Lee [Conjuntos de datos y divisiones](/docs/concepts/datasets).

### Ejecutar las pruebas del repositorio

La entrada `[run]` ejecuta las pruebas. El evaluador lee el código de salida y la salida de las pruebas.

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": output["stdout"][-2000:]}
```

Skynet da por sí mismo un 0 a una versión cuando su entrada `[run]` falla. Si la entrada no arranca, supera el tiempo límite o una señal la detiene, el comentario es `Run failed: …`, y Skynet no llama al evaluador. Un código de salida distinto de cero no es un fallo: el evaluador lo recibe en `output`.

### Modelo como juez

El evaluador puede llamar a un modelo. Importa `llm` desde el módulo auxiliar `skynet`. La llamada pasa por la pasarela de modelos de Skynet y va al modelo evaluador de la ejecución.

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from skynet import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

El evaluador también puede usar cualquier cliente compatible con OpenAI con las variables de entorno `OPENAI_BASE_URL`, `APP_API_KEY` y `APP_MODEL`. La pasarela solo acepta el modelo evaluador de la ejecución.

## Llamadas al modelo y secretos

- Las llamadas con las claves de Skynet se miden contra el presupuesto de la ejecución. Con tu propia clave (BYOK), la llamada va a la clave de tu proveedor, sin tarifa de modelo de Skynet. Lee [Facturación](/docs/concepts/billing).
- El evaluador no recibe los secretos de la ejecución como variables de entorno. Un secreto llega al evaluador solo a través de una tabla `[services.<name>]` que `[metric] services` incluye. Escribe el secreto en los `headers` de la tabla como `${NAME}`. El evaluador envía sus solicitudes a la URL de `SKYNET_SERVICE_<NAME>_URL`. Skynet añade el secreto fuera del sandbox.
- Skynet oculta los valores de los secretos en la salida del evaluador y en los registros.

## Límites

| Elemento                         | Valor                                    |
| -------------------------------- | ---------------------------------------- |
| Tiempo límite del evaluador      | 60 segundos por defecto, como máximo 600 |

## Páginas relacionadas

- [Conjuntos de datos y divisiones](/docs/concepts/datasets)
- [Motores](/docs/concepts/engines)
- [Iniciar una ejecución con el asistente](/docs/guides/submit)
