Un motor es el método de búsqueda que propone nuevas versiones de tu repositorio. Skynet tiene seis motores nativos y **Auto**. Todos los motores trabajan con repositorios. Esta página explica qué hace cada uno, cuándo elegirlo y qué ajustes tiene.

Lee primero [Cómo funciona la optimización](/docs/concepts/optimization) si no sabes qué motor encaja con tu tarea.

## Comparación rápida

| Motor        | Ajustes del agente de código          | Ajuste de iteraciones máx. |
| ------------ | ------------------------------------- | -------------------------- |
| Auto         | No                                    | No                         |
| GEPA         | No                                    | No                         |
| Best-of-N    | No                                    | No                         |
| AutoResearch | Sí                                    | No                         |
| Meta-Harness | Sí                                    | Sí                         |
| AutoSaddler  | Sí                                    | Sí                         |
| ShinkaEvolve | No. Tiene su propio ajuste de editor. | Sí                         |

El asistente marca GEPA y Auto como optimizadores que usan el texto de los comentarios de tu evaluador.

## Auto

Auto es la opción predeterminada para todas las ejecuciones. Trabaja en dos fases:

1. Explora con GEPA, AutoResearch, Meta-Harness, AutoSaddler y ShinkaEvolve.
2. Continúa GEPA desde la mejor versión que encontró la exploración.

**Elígelo cuando** no sepas qué motor encaja. Es el mejor punto de partida.

## GEPA

«Evolución reflexiva con un frente de Pareto de versiones». El modelo de reflexión lee la puntuación y los comentarios de cada caso y escribe una versión mejor. GEPA mantiene un frente de versiones, cada una de las cuales es la mejor en algunos casos.

**Elígelo cuando** tu evaluador dé comentarios concretos y quieras un único motor potente.

## Best-of-N

«Propuestas independientes del modelo de reflexión; conserva la mejor». Cada propuesta es independiente de las demás. Skynet vuelve a puntuar las mejores propuestas con los casos de validación.

**Elígelo cuando** quieras una línea base sencilla para compararla con otros motores.

## AutoResearch

Un agente de código hace rondas de investigación. Edita, puntúa y conserva lo que ayuda hasta que se agota el presupuesto.

**Elígelo cuando** un buen cambio requiera varios pasos de investigación.

## Meta-Harness

Un agente de código propone un lote de candidatos en cada iteración. Usa el historial completo de candidatos y puntuaciones.

**Elígelo cuando** quieras un agente que aprenda del historial de todos los intentos anteriores. Usa **Candidatos por iteración** para fijar el tamaño del lote.

## AutoSaddler

Un agente de código lee los comentarios de tu evaluador y aplica parches a la versión. Skynet solo conserva los parches que confirma una nueva ejecución del evaluador.

**Elígelo cuando** los comentarios de tu evaluador señalen defectos claros y localizados.

## ShinkaEvolve

«Evoluciona muchas versiones en islas paralelas y aprende qué modelos las mejoran más». Es una búsqueda evolutiva sobre una población de candidatos, con mutaciones y cruces que escribe un modelo.

**Elígelo cuando** quieras una exploración amplia y tengas un presupuesto mayor.

## Ajustes

### Presupuesto

| Ajuste                                  | Valor predeterminado | Rango                                                             |
| --------------------------------------- | -------------------- | ----------------------------------------------------------------- |
| Máximo de ejecuciones del evaluador     | 200                  | De 1 a 100000                                  |
| Detener al llegar a la puntuación       | Desactivado          | 0 o más                                                           |
| Iteraciones máx.                        | Desactivado          | De 1 a 1000. Solo Meta-Harness, AutoSaddler y ShinkaEvolve.       |

Una ejecución del evaluador es una llamada de tu evaluador sobre un caso. La nueva puntuación final de la versión inicial y de la ganadora no cuenta para **Máximo de ejecuciones del evaluador**.

### Ajustes del agente de código

Estos ajustes se aplican a AutoResearch, Meta-Harness y AutoSaddler.

| Ajuste                                   | Valor predeterminado | Rango                                                     |
| ---------------------------------------- | -------------------- | --------------------------------------------------------- |
| Agente de código                         | Codex                | Pi, Codex, OpenCode, OpenCode V2, Prime Agent, Aider, OpenHands, OpenHands SDK, Antigravity SDK, Goose, Hermes Agent, eve, DeerFlow, Cline, Code Puppy, comando personalizado   |
| Candidatos por iteración                 | 3                    | De 1 a 8. |
| Máximo de llamadas a herramientas por candidato | 100           | De 1 a 1000                                               |

### Ajustes de ShinkaEvolve

| Ajuste                                          | Valor predeterminado                                                    |
| ----------------------------------------------- | ----------------------------------------------------------------------- |
| Cómo se escriben las versiones nuevas           | Una sola llamada al modelo. La otra opción es «Agente de código (Pi)».  |
| Islas                                           | 2 (de 1 a 8)                                                            |
| Intervalo de migración                          | 10                                                                      |
| Selección de padres                             | Ponderada. Otras opciones: ley de potencias, búsqueda en haz.           |
| Ediciones diff / Reescrituras completas / Cruces | 60 % / 30 % / 10 %. Deben sumar 100 %.                                 |
| Tamaño del archivo                              | 40                                                                      |
| Evaluaciones en paralelo                        | 2                                                                       |
| Propuestas en paralelo                          | 2                                                                       |

## Páginas relacionadas

- [Cómo funciona la optimización](/docs/concepts/optimization)
- [Métricas y evaluadores](/docs/concepts/scorers)
- [Ejecuciones](/docs/concepts/runs)
- [Facturación](/docs/concepts/billing)
