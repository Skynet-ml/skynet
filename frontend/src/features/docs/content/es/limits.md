Skynet fija límites para las ejecuciones, los archivos, el almacenamiento y la frecuencia de solicitudes. Los límites mantienen estable la plataforma y protegen tu saldo. Esta página enumera los valores predeterminados. Un administrador puede cambiar algunos límites de una cuenta.

> [!NOTE]
> Los valores de esta página son los valores predeterminados actuales. Cuando alcanzas un límite, la aplicación o la API te indica qué límite detuvo la solicitud.

## Ejecuciones

| Límite                              | Free | Pro   |
| ----------------------------------- | ---- | ----- |
| Ejecuciones activas al mismo tiempo | 5    | 20    |
| Ejecuciones guardadas               | 100  | 1.000 |

Una ejecución activa es una ejecución con el estado `pending`, `validating`, `running` o `paused`. Cuando tienes el número máximo de ejecuciones activas, Skynet rechaza ejecuciones nuevas hasta que una de ellas termine.

El límite de ejecuciones guardadas cuenta todas tus ejecuciones. Elimina las ejecuciones que no necesites para hacer sitio a otras nuevas.

Si falla la renovación de Pro, la cuenta conserva los límites de Pro durante 7 días. Después usa los límites de Free hasta que el pago se complete.

| Límite                                              | Valor                                                                 |
| --------------------------------------------------- | --------------------------------------------------------------------- |
| Copias en una solicitud de clonación de la API      | De 1 a 5                                                              |
| Intentos de recuperación automática de una ejecución | 3                                                                    |
| Casos de muestra en la comprobación del contrato    | 3                                                                     |
| Eventos de progreso guardados por ejecución         | 5.000. Skynet elimina primero los eventos más antiguos.               |
| Duración de un sandbox                              | 45 minutos. Una ejecución que necesita más tiempo abre un sandbox nuevo. |

Lee [Ejecuciones](/docs/concepts/runs) para saber cómo funcionan los intentos de recuperación.

## Almacenamiento

| Límite                  | Free    | Pro    |
| ----------------------- | ------- | ------ |
| Almacenamiento total    | 500 MiB | 20 GiB |

El almacenamiento total incluye tus ejecuciones, conjuntos de datos, registros, chats con el agente, archivos subidos pendientes y datos de búsqueda. Skynet rechaza una escritura que superaría tu límite de almacenamiento.

## Archivos y conjuntos de datos

| Límite                                                 | Valor                                      |
| ------------------------------------------------------ | ------------------------------------------ |
| Archivo de conjunto de datos guardado en tu biblioteca | 50 MiB por archivo, tras la compresión     |
| Filas de un conector que importa registros             | 20.000                                     |
| Archivo adjunto a un chat con el agente                | 20 MiB                                     |
| Texto leído de un adjunto del chat                     | 50.000 caracteres                          |
| Clip de audio para dictado                             | 25 MB                                      |
| Archivo comprimido de un repositorio de GitHub         | 200 MiB, comprimido                        |
| Archivo del repositorio abierto en la página de ejecución | 1 MiB                                   |

Los conectores que importan registros incluyen tablas de bases de datos, trazas de LLM y bases de datos de Notion. Lee [Preparar un conjunto de datos](/docs/guides/datasets).

### Archivos del espacio de trabajo en el asistente

| Límite                                  | Valor                                                                   |
| --------------------------------------- | ----------------------------------------------------------------------- |
| Archivos adicionales en el espacio de trabajo | 20                                                                |
| Longitud del nombre de un archivo       | 120 caracteres                                                          |
| Profundidad de carpetas                 | 3 carpetas                                                              |
| Tipos de archivo                        | `.py`, `.txt`, `.md`, `.json`, `.yaml`, `.csv`, `.toml`, `.ini`, `.cfg` |

`scorer.py` contiene tu evaluador. Skynet llama a su función `score(repo_path, case)` para cada caso.

## Frecuencia de solicitudes

| Límite                                                                              | Valor                                                  |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Envíos de ejecuciones                                                               | 30 por cuenta en un minuto móvil                       |
| Clips de dictado                                                                    | 120 por cuenta en una hora                             |
| Archivos leídos para chats con el agente                                            | 300 por cuenta en una hora                             |
| Solicitudes de cuenta (registro, restablecimiento de contraseña, verificación de correo) | 20 por dirección de correo en una hora móvil       |
| Turnos del asistente de IA durante el envío                                         | 50 por cuenta en un día UTC                            |
| Tokens de búsqueda e indexación                                                     | 2.000.000 por cuenta en un mes                         |

Cuando superas un límite de frecuencia, la API devuelve el estado HTTP `429`. Espera y vuelve a enviar la solicitud.

Cuando una cuenta supera sus tokens mensuales de búsqueda e indexación, Skynet deja de indexar elementos nuevos de esa cuenta. A partir de entonces, la búsqueda funciona como si la búsqueda semántica no estuviera disponible.

## Dinero

| Límite                                                      | Valor                                                                     |
| ----------------------------------------------------------- | ------------------------------------------------------------------------- |
| Límite de gasto predeterminado de una ejecución nueva       | 5 $, o la cantidad necesaria para empezar si es mayor                     |
| Margen en la cantidad necesaria para empezar                | 1 $, o el coste de la llamada individual al modelo más cara si es mayor   |
| Recarga personalizada                                       | De 5 $ a 1.000 $                                                          |
| Saldo necesario para iniciar un turno interactivo del modelo | 0,05 $                                                                   |

Lee [Presupuestos y facturación](/docs/concepts/billing) para conocer las tarifas y las reservas.

## Páginas relacionadas

- [Ejecuciones](/docs/concepts/runs)
- [Presupuestos y facturación](/docs/concepts/billing)
- [Preguntas frecuentes](/docs/faq)
