Skynet es Optimize Anything: un producto que mejora un programa DSPy o un repositorio según tu métrica. El tipo de artefacto decide qué motores pueden trabajar con él.

## El contrato común

Cada ejecución tiene dos archivos. `run.py` ejecuta el artefacto sobre un caso. `metric.py` puntúa la salida.

```python tab="run.py"
from skynet import llm


def run(artifact, input):
    return llm(artifact, input=input)
```

```python tab="metric.py"
def score(input, output, expected=None):
    ok = output == expected
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `artifact` es la versión actual que propone el optimizador.
- `input` es un dict que contiene los campos de entrada de un caso.
- `expected` es el valor esperado del caso, o `None` cuando el caso no tiene ninguno.
- `score` devuelve un par: un número y un dict con una cadena `"feedback"`.

El módulo auxiliar `skynet` ofrece `llm`, `Image` y `log`. Una llamada a `llm` desde el código de ejecución va al modelo de tarea. Una llamada a `llm` desde la métrica va al modelo de la métrica.

> [!NOTE]
> El código de ejecución se ejecuta en su propio proceso dentro del sandbox. No puede ver el código fuente de la métrica, el valor esperado ni la clave de la métrica.

Lee [Métricas y evaluadores](/docs/concepts/scorers) para conocer el contrato completo de la métrica.

## Tipos de artefacto y motores

| Tipo de artefacto | Motores                                                                      |
| ----------------- | ---------------------------------------------------------------------------- |
| Repositorio | GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve |
| Programa DSPy     | Auto, DSPy GEPA, DSPy MIPROv2, DSPy BootstrapFewShot                         |

Los motores se agrupan en dos familias:

- Los **motores nativos** tratan el artefacto como una caja negra. Trabajan con repositorios.
- Los **optimizadores de DSPy** trabajan dentro de un programa DSPy. Solo trabajan con artefactos de tipo Programa DSPy.

DSPy MIPROv2 y DSPy BootstrapFewShot necesitan pares (casos con salidas esperadas). Con una única entrada, un programa DSPy solo puede usar Auto y DSPy GEPA.

## Repositorios

Los motores nativos cambian los archivos de las rutas que permites. Tu métrica puntúa cada versión del repositorio.

Los seis motores nativos usan métodos de búsqueda distintos:

- **GEPA** hace evolucionar las versiones mediante reflexión y mantiene un frente de Pareto.
- **Best-of-N** pide al modelo de reflexión propuestas independientes y conserva la mejor.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** y **ShinkaEvolve** usan un agente de código para proponer versiones.

Lee [Motores](/docs/concepts/engines) para conocer cada motor y sus ajustes.

## Artefactos de tipo Programa DSPy

Un programa DSPy es un tipo de artefacto. Tu `run.py` construye un `dspy.Module` a nivel de módulo y lo llama dentro de `run`. Si el archivo tiene más de un módulo, llama `program` al que quieres optimizar.

Qué ocurre cuando se ejecuta un optimizador de DSPy:

1. Skynet encuentra tu módulo y envuelve tu función `run` a su alrededor. Cada caso pasa por tu propio `run`.
2. El optimizador de DSPy compila el programa con los casos de entrenamiento.
3. Selecciona con los casos de validación.
4. El ganador es el texto de las instrucciones de cada predictor, más el estado compilado completo del módulo. El estado incluye las demostraciones few-shot.

Las puntuaciones finales miden el mismo programa que guarda Skynet.

| Optimizador           | Lee el texto de los comentarios |
| --------------------- | ------------------------------- |
| DSPy GEPA             | Sí                              |
| DSPy MIPROv2          | No, solo la puntuación          |
| DSPy BootstrapFewShot | No, solo la puntuación          |

DSPy BootstrapFewShot no usa un modelo de reflexión.

> [!TIP]
> El modelo de reflexión de DSPy solo ve el texto de los comentarios que devuelve tu métrica. Nunca ve el código fuente de la métrica.

## Auto

**Auto** es la opción predeterminada en el asistente.

- **Repositorio**: Auto no está disponible. Selecciona un motor nativo.
- En un programa DSPy, Auto ejecuta DSPy GEPA.

Auto lee el texto de feedback de tu métrica.

## Qué motor elegir

| Tu situación                                                   | Usa                                                                         |
| -------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Tienes código en un repositorio de GitHub | Un repositorio. Selecciona un motor nativo. |
| Tienes un programa DSPy                                        | Un artefacto de tipo Programa DSPy. Empieza con Auto.                       |
| Quieres demostraciones few-shot elegidas de tus datos          | Un artefacto de tipo Programa DSPy con DSPy BootstrapFewShot o DSPy MIPROv2 |

> [!WARNING]
> GEPA, DSPy GEPA y Auto aprenden del texto de los comentarios. Si tus comentarios son siempre la misma cadena, estos optimizadores tienen menos información con la que trabajar.

## Páginas relacionadas

- [Motores](/docs/concepts/engines)
- [Conjuntos de datos y divisiones](/docs/concepts/datasets)
- [Métricas y evaluadores](/docs/concepts/scorers)
- [Iniciar una ejecución con el asistente](/docs/guides/submit)
