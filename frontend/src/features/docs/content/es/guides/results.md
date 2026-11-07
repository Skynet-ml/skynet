La página de la ejecución muestra el progreso y los resultados de una ejecución. Esta guía explica las pestañas de la página de la ejecución, las puntuaciones y cómo exportar o servir la mejor versión.

## Pestañas de la página de la ejecución

La página de la ejecución solo muestra las pestañas que se aplican a la ejecución. Algunas pestañas dependen del estado y de tu nivel de experiencia.

| Pestaña                  | Cuándo aparece                                                   | Qué contiene                                                                         |
| ------------------------ | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| **Resumen**              | Siempre                                                          | El progreso, las puntuaciones y el historial de puntuaciones                         |
| **Uso**                  | Una ejecución correcta cuyo repositorio tiene un archivo `serve.py` | Un formulario para llamar a la mejor versión, y código de integración             |
| **Artefacto**            | Una ejecución con resultado                                      | Las versiones de tu repositorio, con la mejor versión                                |
| **Datos**                | Una ejecución terminada con casos                                | La división de los datos en los conjuntos de entrenamiento, validación y prueba      |
| **Código**               | Cuando la ejecución tiene código que mostrar                     | El evaluador                                                                         |
| **Recompensa**           | Una ejecución cuyo evaluador declara partes de recompensa        | Las partes de la recompensa y sus pesos                                              |
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

Cuando Skynet crea el repositorio para la ejecución, sube cada candidato a su propia rama, `skynet/candidate-<version id>`. La rama `skynet/best` siempre apunta a la mejor versión. Se mueve cuando aparece una versión mejor.

## Servir la mejor versión

Puedes llamar a la mejor versión desde tu propio código. El repositorio debe tener en su raíz un archivo `serve.py` que defina `serve(input)`:

```python tab="serve.py"
def serve(input):
    return {"label": classify(input["message"])}
```

- `input` es el dict de campos de entrada de la solicitud.
- El valor devuelto debe poder serializarse a JSON. Un dict se convierte en `outputs`. Cualquier otro valor se convierte en `{"output": value}`.
- `serve.py` puede llamar a un modelo con `llm` del módulo auxiliar `skynet`. No recibe los secretos de la ejecución.

Skynet ejecuta la mejor versión en el mismo sandbox protegido que la ejecución. La pestaña **Uso** tiene dos partes:

- Un formulario. Introduce los campos de entrada y selecciona **Ejecutar** para obtener un resultado.
- **Código de integración** en cURL, Python, JavaScript y Go.

Para llamar a la mejor versión, crea un token de API en **Ajustes → API**. Después envía una solicitud. La cabecera `Idempotency-Key` es obligatoria:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5}'
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
    json={"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5},
)
print(response.json()["outputs"])
```

Sustituye `message` por los campos de entrada de tu ejecución. El **Código de integración** de la pestaña **Uso** rellena por ti la URL real y los nombres de los campos.

La respuesta contiene `outputs`, los `input_fields`, los `output_fields`, el `model_used`, `cents_charged` y `budget`.

> [!TIP]
> Cada llamada se cobra por separado, del saldo de quien llama. `max_cost_cents` fija lo máximo que puede costar esta llamada, en centavos. Si envías otra vez la misma `Idempotency-Key`, Skynet no vuelve a cobrar la llamada.

Lee [API](/docs/api) para conocer la autenticación y los demás endpoints.

## Compartir los resultados

Puedes compartir una ejecución con otras personas, o mostrarla en la galería pública Explorar. Lee [Compartir y publicar](/docs/guides/sharing).
