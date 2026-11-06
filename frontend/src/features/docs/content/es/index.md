Skynet mejora un artefacto que ya tienes. Le das una versión inicial, un conjunto de casos y una métrica. Skynet propone versiones nuevas, ejecuta cada una sobre tus casos, puntúa la salida con tu métrica y conserva la mejor versión.

## Qué optimiza Skynet

Skynet trabaja con dos tipos de artefacto:

| Tipo de artefacto | Qué cambia                                                     |
| ----------------- | -------------------------------------------------------------- |
| Repositorio | Los archivos de las rutas que permites cambiar |
| Programa DSPy     | Las instrucciones de los predictores de un `dspy.Module`       |

Todas las ejecuciones usan el mismo contrato. Tu código de ejecución define `run(artifact, input)`. Tu métrica define `score(input, output, expected=None)`. La métrica devuelve una puntuación y comentarios por escrito. El optimizador lee los comentarios para que la siguiente versión sea mejor.

> [!NOTE]
> Tu código de ejecución y tu métrica se ejecutan en un sandbox, en procesos separados. El código de ejecución no puede ver el código fuente de la métrica ni los valores esperados.

## Un producto, muchos motores

Skynet es Optimize Anything. El tipo de artefacto decide qué motores pueden trabajar con él:

- Los repositorios usan los motores nativos: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler y ShinkaEvolve.
- Los artefactos de tipo Programa DSPy usan los optimizadores de DSPy: DSPy GEPA, DSPy MIPROv2 y DSPy BootstrapFewShot.

En un programa DSPy, **Auto** usa DSPy GEPA. En un repositorio, eliges un motor nativo. Lee [Cómo funciona la optimización](/docs/concepts/optimization) y [Motores](/docs/concepts/engines).

## Cómo funciona una ejecución

1. Describes la tarea y defines la versión inicial.
2. Aportas los casos. Skynet los divide en los conjuntos de entrenamiento, validación y prueba.
3. Seleccionas un optimizador, los modelos y un presupuesto.
4. Escribes el código de ejecución y la métrica. Una comprobación del contrato los ejecuta sobre casos de muestra.
5. Skynet ejecuta la optimización y muestra los resultados.

Skynet selecciona las versiones con los casos de validación. Informa la puntuación final con los casos de prueba. Los motores nunca ven los casos de prueba.

## Por dónde empezar

| Si quieres                                | Lee                                                    |
| ----------------------------------------- | ------------------------------------------------------ |
| Iniciar tu primera ejecución              | [Inicio rápido](/docs/quickstart)                      |
| Entender cómo funciona la optimización    | [Cómo funciona la optimización](/docs/concepts/optimization) |
| Elegir un motor                           | [Motores](/docs/concepts/engines)                      |
| Preparar tus datos                        | [Conjuntos de datos y divisiones](/docs/concepts/datasets) |
| Escribir una métrica                      | [Métricas y evaluadores](/docs/concepts/scorers)       |
| Completar el asistente                    | [Iniciar una ejecución con el asistente](/docs/guides/submit)            |
| Leer tus resultados                       | [Resultados](/docs/guides/results)                     |
| Entender el coste                         | [Facturación](/docs/concepts/billing)                  |
| Usar Skynet desde un agente               | [MCP](/docs/mcp)                                       |
| Llamar a Skynet desde código              | [API](/docs/api)                                       |
| Consultar los límites                     | [Límites](/docs/limits)                                |

> [!TIP]
> Empieza con un conjunto de datos pequeño y la configuración predeterminada. Haz que los comentarios de la métrica sean concretos. Unos buenos comentarios le dan al optimizador más información que la puntuación sola.
