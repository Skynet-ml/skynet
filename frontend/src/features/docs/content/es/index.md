Skynet mejora código que ya tienes, o código que escribe para ti. Skynet es Optimize Anything: cada ejecución optimiza un repositorio. Le das un repositorio, un conjunto de casos y un evaluador. Skynet propone versiones nuevas del repositorio, puntúa cada una con tu evaluador y conserva la mejor versión.

## Qué optimiza Skynet

Cada ejecución optimiza un repositorio. Puedes empezar de dos formas:

| Inicio                   | Qué pasa                                                                                        |
| ------------------------ | ----------------------------------------------------------------------------------------------- |
| Usar un repositorio      | Skynet cambia los archivos de las rutas que permites, en un repositorio de GitHub que conectas. |
| Crear un repositorio nuevo | Das una tarea y datos. Skynet crea un repositorio nuevo de GitHub en tu cuenta y escribe una primera solución. |

El código del repositorio puede ser cualquier cosa: un pipeline de prompts, un harness de agente, un script o una biblioteca. Puede usar cualquier biblioteca, por ejemplo DSPy.

Todas las ejecuciones usan el mismo contrato. Tu evaluador define `score(repo_path, case)`. Devuelve una puntuación y comentarios por escrito. El optimizador lee los comentarios para que la siguiente versión sea mejor.

> [!NOTE]
> Cada versión se ejecuta en un sandbox. El agente de código que escribe las versiones no puede ver el código fuente del evaluador ni los valores esperados.

## Un producto, muchos motores

**Auto** es el valor predeterminado de cada ejecución. Prueba GEPA, AutoResearch, Meta-Harness, AutoSaddler y ShinkaEvolve, y después continúa con GEPA desde la mejor versión. También puedes seleccionar un motor: GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler o ShinkaEvolve. Lee [Cómo funciona la optimización](/docs/concepts/optimization) y [Motores](/docs/concepts/engines).

## Cómo funciona una ejecución

1. Describes la tarea y eliges un repositorio o pides a Skynet que cree uno.
2. Aportas los casos. Skynet los divide en los conjuntos de entrenamiento, validación y prueba.
3. Seleccionas un optimizador, los modelos y un presupuesto.
4. Escribes el evaluador. Una comprobación de configuración lo ejecuta sobre la versión inicial.
5. Skynet ejecuta la optimización y muestra los resultados.

Skynet selecciona las versiones con los casos de validación. Informa la puntuación final con los casos de prueba. Los motores nunca ven los casos de prueba.

## Por dónde empezar

| Si quieres                                | Lee                                                    |
| ----------------------------------------- | ------------------------------------------------------ |
| Iniciar tu primera ejecución              | [Inicio rápido](/docs/quickstart)                      |
| Entender cómo funciona la optimización    | [Cómo funciona la optimización](/docs/concepts/optimization) |
| Elegir un motor                           | [Motores](/docs/concepts/engines)                      |
| Preparar tus datos                        | [Conjuntos de datos y divisiones](/docs/concepts/datasets) |
| Escribir un evaluador                     | [Métricas y evaluadores](/docs/concepts/scorers)       |
| Completar el asistente                    | [Iniciar una ejecución con el asistente](/docs/guides/submit)            |
| Leer tus resultados                       | [Resultados](/docs/guides/results)                     |
| Entender el coste                         | [Facturación](/docs/concepts/billing)                  |
| Usar Skynet desde un agente               | [MCP](/docs/mcp)                                       |
| Llamar a Skynet desde código              | [API](/docs/api)                                       |
| Consultar los límites                     | [Límites](/docs/limits)                                |

> [!TIP]
> Empieza con un conjunto de datos pequeño y la configuración predeterminada. Haz que los comentarios del evaluador sean concretos. Unos buenos comentarios le dan al optimizador más información que la puntuación sola.
