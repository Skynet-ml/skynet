Skynet es Optimize Anything: cada ejecución optimiza un repositorio. Un motor propone versiones nuevas del repositorio, tu evaluador puntúa cada versión y Skynet conserva la mejor.

## El contrato común

Cada ejecución tiene un repositorio y un evaluador. El evaluador es una función de Python que recibe la ruta de una versión del repositorio y un caso.

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.serve(case["input"])
    ok = output == case["expected"]
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `repo_path` es la carpeta de una versión del repositorio, después de ejecutar el comando de configuración.
- `case` es un dict. `case["input"]` contiene los campos de entrada de un caso. `case["expected"]` contiene el valor esperado, o `None` cuando el caso no tiene ninguno.
- `score` devuelve un par: un número y un dict con una cadena `"feedback"`.

El repositorio tiene un archivo `serve.py` en su raíz con `def serve(input)`. El evaluador puede llamarlo, y Skynet lo usa para servir la mejor versión después de la ejecución.

> [!NOTE]
> El agente de código que escribe las versiones no puede ver el código fuente del evaluador, los valores esperados ni los secretos de la ejecución.

Lee [Métricas y evaluadores](/docs/concepts/scorers) para conocer el contrato completo del evaluador.

## Llamadas al modelo desde tu código

El código de tu repositorio y tu evaluador pueden llamar a un modelo a través de la pasarela de modelos de Skynet. El sandbox da un endpoint compatible con OpenAI en variables de entorno:

| Variable             | Qué contiene                                                               |
| -------------------- | -------------------------------------------------------------------------- |
| `OPENAI_BASE_URL`    | La URL de la pasarela. `APP_GATEWAY_URL` contiene el mismo valor.          |
| `APP_API_KEY`        | Un token solo para esta ejecución. No es una clave de proveedor.           |
| `APP_MODEL`          | El nombre del modelo que debes enviar. Es el modelo evaluador de la ejecución. |
| `ANTHROPIC_BASE_URL` | La URL de la pasarela para los SDK de Anthropic.                           |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["APP_API_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["APP_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- Las llamadas con las claves de Skynet se miden contra el presupuesto de la ejecución.
- Con tu propia clave (BYOK), la llamada va a la clave de tu proveedor. Skynet no cobra ninguna tarifa por el modelo.
- La pasarela rechaza las solicitudes para cualquier otro modelo.
- Los secretos de la ejecución llegan a tu código y a tu evaluador como variables de entorno. El nombre de la variable es el nombre del secreto. Skynet oculta los valores de los secretos en la salida.

El sandbox no tiene otro acceso a la red.

## Motores

Todos los motores trabajan con repositorios:

- **Auto** es la opción predeterminada. Prueba varios motores y después continúa GEPA desde la mejor versión.
- **GEPA** hace evolucionar las versiones por reflexión y mantiene un frente de Pareto.
- **Best-of-N** pide al modelo de reflexión propuestas independientes y conserva la mejor.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** y **ShinkaEvolve** usan un agente de código para proponer versiones.

Los motores solo cambian los archivos de las rutas que permites. Lee [Motores](/docs/concepts/engines) para conocer cada motor y sus ajustes.

## Auto

**Auto** es la opción predeterminada en el asistente, para todas las ejecuciones.

1. Auto explora con cinco motores: GEPA, AutoResearch, Meta-Harness, AutoSaddler y ShinkaEvolve.
2. Elige la mejor versión que encontraron.
3. Continúa GEPA desde esa versión hasta que se agota el presupuesto.

Auto lee el texto de los comentarios de tu evaluador.

## De dónde sale la versión inicial

| Tu situación                             | Usa                                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------------------ |
| Tienes código en un repositorio de GitHub | **Usar un repositorio**. El código de la rama o el commit que elijas es el punto de partida. |
| Solo tienes una tarea y datos            | **Crear un repositorio nuevo**. Skynet escribe una primera solución. Ese es el punto de partida. |

> [!WARNING]
> GEPA y Auto aprenden del texto de los comentarios. Si tus comentarios siempre son la misma cadena, estos optimizadores tienen menos información con la que trabajar.

## Páginas relacionadas

- [Motores](/docs/concepts/engines)
- [Conjuntos de datos y divisiones](/docs/concepts/datasets)
- [Métricas y evaluadores](/docs/concepts/scorers)
- [Iniciar una ejecución con el asistente](/docs/guides/submit)
