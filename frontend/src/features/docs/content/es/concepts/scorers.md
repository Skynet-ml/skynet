El evaluador le dice a Skynet lo buena que es una versión de tu repositorio. Es una función de Python en `scorer.py`. Todos los motores llaman a la misma función. Esta página explica el contrato, el valor devuelto y algunos ejemplos.

## El contrato

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Argumento   | Qué contiene                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | La carpeta de una versión del repositorio, después de ejecutar el comando de configuración |
| `case`      | Un dict. `case["input"]` contiene los campos de entrada. `case["expected"]` contiene el valor esperado, o `None` |

Si tu función solo recibe un argumento, Skynet llama a `score(repo_path)`. Usa esta forma cuando el evaluador ejecuta sus propias pruebas y no necesita casos.

El evaluador se ejecuta en el sandbox, en la carpeta de la versión. Puede importar código del repositorio, ejecutar un comando o llamar a `serve()` de `serve.py`. Lee [Cómo funciona la optimización](/docs/concepts/optimization).

## El valor devuelto

`score` devuelve un objeto:

```python
{
    "score": 0.78,
    "feedback": "Misses short names.",
    "scores": {
        "precision": {"score": 0.90},
        "recall": {"score": 0.69, "feedback": "Misses names under 4 letters."},
    },
}
```

- `"score"` es obligatorio. Es un número finito. Solo esta puntuación ordena las versiones.
- `"feedback"` es un texto opcional. Se recomienda, porque GEPA y Auto aprenden de él. Sin comentarios, los motores solo ven las puntuaciones, y el registro de la ejecución lo indica una vez.
- `"scores"` es opcional. Asocia nombres a puntuaciones con nombre. Cada puntuación con nombre es un objeto con su propio `"score"` y un `"feedback"` opcional. Todos los motores usan cada puntuación con nombre como un objetivo propio.

Cualquier otra forma o clave es un error. La comprobación muestra la parte incorrecta y la forma válida.

| Valor devuelto | Válido |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | Sí |
| `{"score": 0.8}` | Sí. No hay comentarios. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | Sí |
| `0.8` | No. El valor no es un objeto. |
| `(0.8, "Close. Missing the date.")` | No. El valor no es un objeto. |
| `{"score": "0.8"}` | No. La puntuación es una cadena. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | No. Una puntuación con nombre debe ser un objeto. |
| `{"score": 0.8, "feedbak": "OK"}` | No. La clave `feedbak` es desconocida. |

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

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` es un dict cuya clave es el nombre de tu columna de salida. Lee [Conjuntos de datos y divisiones](/docs/concepts/datasets).

### Ejecutar las pruebas del repositorio

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
```

### Modelo como juez

El evaluador puede llamar a un modelo. Importa `llm` desde el módulo auxiliar `skynet`. La llamada pasa por la pasarela de modelos de Skynet y va al modelo evaluador de la ejecución.

```python tab="scorer.py"
import importlib.util

from skynet import llm


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.serve(case["input"])
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {answer}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

El evaluador también puede usar cualquier cliente compatible con OpenAI con las variables de entorno `OPENAI_BASE_URL`, `APP_API_KEY` y `APP_MODEL`. La pasarela solo acepta el modelo evaluador de la ejecución.

## Llamadas al modelo y secretos

- Las llamadas con las claves de Skynet se miden contra el presupuesto de la ejecución. Con tu propia clave (BYOK), la llamada va a la clave de tu proveedor, sin tarifa de modelo de Skynet. Lee [Facturación](/docs/concepts/billing).
- Los secretos de la ejecución llegan al evaluador como variables de entorno. El nombre de la variable es el nombre del secreto, por ejemplo `os.environ["SEARCH_API_KEY"]`.
- Skynet oculta los valores de los secretos en la salida del evaluador y en los registros.

## Límites

| Elemento                         | Valor                                    |
| -------------------------------- | ---------------------------------------- |
| Tiempo límite del evaluador      | 60 segundos por defecto, como máximo 600 |

## Páginas relacionadas

- [Conjuntos de datos y divisiones](/docs/concepts/datasets)
- [Motores](/docs/concepts/engines)
- [Iniciar una ejecución con el asistente](/docs/guides/submit)
