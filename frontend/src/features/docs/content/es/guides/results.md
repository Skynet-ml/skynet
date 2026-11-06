La página de la ejecución muestra el progreso y los resultados de una ejecución. Esta guía explica las pestañas de la página de la ejecución, las puntuaciones y cómo exportar o servir la mejor versión.

## Pestañas de la página de la ejecución

La página de la ejecución solo muestra las pestañas que se aplican a la ejecución. Algunas pestañas dependen de la ruta, del estado y de tu nivel de experiencia.

| Pestaña                 | Cuándo aparece                                                                 | Qué contiene                                                                                     |
| ----------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| **Resumen**             | Siempre                                                                        | El progreso, las puntuaciones y el historial de puntuaciones                                     |
| **Uso**                 | Una ejecución correcta con un programa que Skynet puede servir                 | Un formulario para ejecutar el programa optimizado, y código de integración                      |
| **Artefacto** | Una ejecución de repositorio con resultado | Las versiones de tu artefacto, con la mejor versión |
| **Datos**               | Una ejecución terminada de un programa DSPy                                    | La división de los datos en los conjuntos de entrenamiento, validación y prueba, y la evaluación del conjunto de prueba |
| **Código**              | Cuando la ejecución tiene código que mostrar                                   | El código de ejecución y la métrica                                                              |
| **Recompensa** | Una ejecución de repositorio cuya métrica declara partes de recompensa | Las partes de la recompensa y sus pesos |
| **Artefacto** | Una ejecución terminada de un programa DSPy | La salida optimizada y **Exportar resultados** |
| **Registros**           | La mayoría de las ejecuciones                                                  | El registro de la ejecución                                                                      |
| **Uso y coste**         | Cuando has iniciado sesión                                                     | Lo que gastó la ejecución                                                                        |
| **Ejecución programada** | Una ejecución correcta de un programa DSPy | Una programación para volver a ejecutar la optimización |
| **Configuración**       | Siempre                                                                        | La configuración de la ejecución                                                                 |

> [!NOTE]
> En el nivel **Guiado**, la página de la ejecución oculta las pestañas **Datos**, **Código**, **Registros** y **Configuración**. Un enlace directo a una de estas pestañas sigue abriéndola. Cambia el nivel en **Ajustes → Cuenta**.

## Puntuaciones

Skynet selecciona las versiones con los casos de validación. Informa la puntuación final con los casos de prueba. Los motores nunca ven los casos de prueba. Lee [Conjuntos de datos y divisiones](/docs/concepts/datasets).

La pestaña **Resumen** muestra:

- **Mejora**: el cambio desde la versión inicial hasta la mejor versión.
- **Progresión de la puntuación**: la puntuación a lo largo de la ejecución.
- **Desglose de la puntuación**: cada métrica, con las columnas **Línea base**, **Optimizado** y **Cambio**.

Mientras la ejecución está activa, el resumen también muestra **Transcurrido**, **Restante** y **Tasa**.

En una ejecución de repositorio, el resumen también muestra **Puntuaciones con nombre** cuando tu métrica informa puntuaciones con nombre.

En una ejecución de un programa DSPy, la pestaña **Datos** tiene una **Evaluación del conjunto de prueba**. Muestra la puntuación de la línea base y la optimizada para cada caso de prueba.

### Cuando la mejor versión es la versión inicial

A veces ningún candidato supera a la versión inicial. Skynet devuelve entonces la versión inicial. La pestaña **Artefacto** te indica que se aplicó una protección contra regresiones.

Si la ejecución se detuvo al llegar a su presupuesto, la página de la ejecución te indica qué versión guardó Skynet. Lee [Ejecuciones](/docs/concepts/runs).

## Exportar la mejor versión

### Ejecuciones de repositorio

La pestaña **Artefacto** muestra las **Versiones** de tu artefacto. Para cada versión puedes:

- Copiar la versión.
- Descargar la versión como archivo.
- En una ejecución de repositorio, descargar la versión como un zip del repositorio completo.

Puedes comparar una versión con otra. La pestaña también muestra los **Comentarios del evaluador** de una versión.

### Ejecuciones de programas DSPy

La pestaña **Artefacto** tiene un panel **Exportar resultados**. Abre el menú **Descargar**. Muestra los formatos que tiene la ejecución:

| Opción              | Formato                                                   |
| ------------------- | --------------------------------------------------------- |
| Programa DSPy       | `.zip`, una exportación autónoma que puedes ejecutar      |
| Programa optimizado | `.pkl`                                                    |
| Prompt              | `.json`                                                   |
| Código optimizado   | `.py`                                                     |
| Registros           | `.csv`                                                    |

## Servir la mejor versión

Puedes llamar a un programa DSPy optimizado desde tu propio código. La pestaña **Uso** tiene dos partes:

- Un formulario. Introduce los campos de entrada y selecciona **Ejecutar** para obtener una predicción.
- **Código de integración** en cURL, Python, JavaScript, Go y DSPy.

El programa siempre se ejecuta con los ajustes de modelo con los que se optimizó. Debes dar todos los campos de entrada. Skynet no tiene en cuenta los campos adicionales.

Para llamar al programa, crea un token de API en **Ajustes → API**. Después envía una solicitud:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"question": "What is DSPy?"}}'
```

```python tab="Python"
import os
import uuid
import requests

response = requests.post(
    f"{os.environ['API_URL']}/serve/<optimization_id>",
    headers={
        "Authorization": f"Bearer {os.environ['APP_API_TOKEN']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={"inputs": {"question": "What is DSPy?"}},
)
print(response.json()["outputs"])
```

Sustituye `question` por los campos de entrada de tu programa. El **Código de integración** de la pestaña **Uso** rellena por ti la URL real y los nombres de los campos.

La respuesta contiene `outputs`, los `input_fields`, los `output_fields` y el `model_used`.

> [!TIP]
> El cuerpo de la solicitud también acepta `max_cost_cents`. Fija lo máximo que puede costar esta llamada, en centavos. Algunas ejecuciones lo exigen.

Para recibir salidas parciales a medida que llegan, envía la misma solicitud a `/serve/<optimization_id>/stream`. La respuesta es un flujo de eventos enviados por el servidor (server-sent events).

Lee [API](/docs/api) para conocer la autenticación y los demás endpoints.

## Compartir los resultados

Puedes compartir una ejecución con otras personas, o mostrarla en la galería pública Explorar. Lee [Compartir y publicar](/docs/guides/sharing).
