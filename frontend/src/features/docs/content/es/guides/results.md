La página de la ejecución muestra el progreso y los resultados de una ejecución. Esta guía explica las pestañas de la página de la ejecución, las puntuaciones y cómo exportar la mejor versión.

## Pestañas de la página de la ejecución

La página de la ejecución solo muestra las pestañas que se aplican a la ejecución. Algunas pestañas dependen del estado y de tu nivel de experiencia.

| Pestaña                  | Cuándo aparece                                                   | Qué contiene                                                                         |
| ------------------------ | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| **Resumen**              | Siempre                                                          | El progreso, las puntuaciones y el historial de puntuaciones                         |
| **Artefacto**            | Una ejecución con resultado                                      | Las versiones de tu repositorio, con la mejor versión                                |
| **Datos**                | Una ejecución terminada con casos                                | La división de los datos en los conjuntos de entrenamiento, validación y prueba      |
| **Código**               | Cuando la ejecución tiene código que mostrar                     | El evaluador                                                                         |
| **Registros**            | La mayoría de las ejecuciones                                    | El registro de la ejecución                                                          |
| **Uso y coste**          | Cuando has iniciado sesión                                       | Lo que gastó la ejecución                                                            |
| **Ejecución programada** | Una ejecución correcta                                           | Una programación para volver a ejecutar la optimización desde la mejor versión       |
| **Configuración**        | Siempre                                                          | La configuración de la ejecución                                                     |

> [!NOTE]
> En el nivel **Guiado**, la página de la ejecución oculta las pestañas **Datos**, **Código**, **Registros** y **Configuración**. Un enlace directo a una de estas pestañas sigue abriéndola. Cambia el nivel en **Ajustes → Cuenta**.

## Puntuaciones

Skynet selecciona las versiones con los casos de validación. Informa la puntuación final con los casos de prueba. Los motores nunca ven los casos de prueba. Lee [Conjuntos de datos y divisiones](/docs/concepts/datasets).

La pestaña **Resumen** muestra:

- **Mejora**: el cambio desde la versión inicial hasta la mejor versión.
- **Progresión de la puntuación**: la puntuación a lo largo de la ejecución.
- **Desglose de la puntuación**: cada puntuación, con las columnas **Línea base**, **Optimizado** y **Cambio**.

Mientras la ejecución está activa, el resumen también muestra **Transcurrido**, **Restante** y **Tasa**.

El resumen también muestra **Puntuaciones con nombre** cuando tu evaluador informa puntuaciones con nombre.

### Cuando la mejor versión es la versión inicial

A veces ningún candidato supera a la versión inicial. Skynet devuelve entonces la versión inicial. La pestaña **Artefacto** te indica que se aplicó una protección contra regresiones.

Si la ejecución se detuvo al llegar a su presupuesto, la página de la ejecución te indica qué versión guardó Skynet. Lee [Ejecuciones](/docs/concepts/runs).

## Exportar la mejor versión

La pestaña **Artefacto** muestra las **Versiones** de tu repositorio. Para cada versión puedes:

- Copiar la versión.
- Descargar la versión como archivo.
- Descargar la versión como un zip del repositorio completo.

Puedes comparar una versión con otra. La pestaña también muestra los **Comentarios del evaluador** de una versión.

### Ramas en un repositorio nuevo

Cuando Skynet crea el repositorio para la ejecución en GitHub, sube cada candidato a su propia rama, `skynet/candidate-<version id>`. La rama `skynet/best` siempre apunta a la mejor versión. Se mueve cuando aparece una versión mejor.

## Compartir los resultados

Puedes compartir una ejecución con otras personas, o mostrarla en la galería pública Explorar. Lee [Compartir y publicar](/docs/guides/sharing).
