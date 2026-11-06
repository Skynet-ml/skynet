La API HTTP de Skynet permite que scripts y herramientas hagan lo mismo que la aplicación web: iniciar ejecuciones, seguirlas y leer los resultados. Esta página muestra cómo firmar las solicitudes, dónde está la referencia completa y un ejemplo completo.

> [!TIP]
> ¿Usas un agente de IA como Claude Code, Codex o Cursor? Conéctalo mediante [MCP](/docs/mcp). Así el agente inicia y sigue las ejecuciones por ti, con un límite de gasto para cada ejecución.

## URL base

Envía todas las solicitudes a:

```text
https://api.skynetml.com
```

## Autenticación

La API usa tokens de API.

1. Abre **Ajustes** en Skynet y luego la pestaña **API**.
2. Haz clic en **Generar token**.
3. Copia el token en ese momento. Skynet muestra el token completo una sola vez.

Envía el token en la cabecera `Authorization` de cada solicitud:

```http
Authorization: Bearer skyd_...
```

- Los tokens empiezan por `skyd_`.
- Cada usuario tiene como máximo un token válido. Un token nuevo invalida el anterior.
- Haz clic en **Revocar token** para invalidar un token.

> [!WARNING]
> Trata el token como una contraseña. Mantenlo fuera del código fuente y de los registros. Lee [Seguridad](/docs/security) para saber cómo guarda Skynet los tokens.

## Referencia interactiva

La referencia completa está en [api.skynetml.com/scalar](https://api.skynetml.com/scalar). Enumera cada endpoint público, sus campos y sus respuestas. Desde esa página puedes enviar solicitudes de prueba.

La referencia lee el documento OpenAPI en `https://api.skynetml.com/openapi.public.json`. Usa ese archivo para generar un cliente en tu lenguaje.

## Endpoints principales

| Método y ruta                                   | Qué hace                                                       |
| ----------------------------------------------- | -------------------------------------------------------------- |
| `GET /models`                                   | Enumera los modelos que puedes usar.                           |
| `GET /optimizers`                               | Enumera los optimizadores que encajan con una configuración.   |
| `GET /blackbox/engines`                         | Enumera los motores nativos e indica si cada uno está disponible. |
| `POST /blackbox/run`                            | Inicia una ejecución sobre un repositorio de GitHub.           |
| `GET /optimizations`                            | Enumera tus ejecuciones.                                       |
| `GET /optimizations/{optimization_id}`          | Devuelve una ejecución: estado, progreso, registros y resultado. |
| `GET /optimizations/{optimization_id}/artifact` | Devuelve la mejor versión que encontró la ejecución.           |
| `POST /optimizations/{optimization_id}/cancel`  | Detiene una ejecución.                                         |
| `POST /serve/{optimization_id}`                 | Ejecuta la mejor versión con una entrada nueva.                |
| `GET /health`                                   | Indica si el servicio está activo.                             |

## Iniciar una ejecución

Skynet es Optimize Anything: cada ejecución optimiza un repositorio. `POST /blackbox/run` recibe la misma configuración que el asistente de envío. El ejemplo mejora un repositorio que clasifica mensajes de soporte en etiquetas.

```bash tab="curl"
curl https://api.skynetml.com/blackbox/run \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "objective": "Sort each support message into billing, bug or other.",
  "recipe": "repo",
  "target": {
    "kind": "repo",
    "repo": {
      "provider": "github",
      "repository": "your-org/support-labels",
      "branch": "main",
      "editable_paths": ["serve.py", "prompts/"]
    }
  },
  "cases": [
    { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
    { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
    {
      "input": { "message": "Do you have a Hebrew interface?" },
      "expected": { "label": "other" }
    }
  ],
  "input_fields": ["message"],
  "expected_field": "label",
  "scorer": {
    "kind": "python",
    "metric_code": "import importlib.util\n\n\ndef score(repo_path, case):\n    spec = importlib.util.spec_from_file_location(\"serve\", f\"{repo_path}/serve.py\")\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    ok = str(module.serve(case[\"input\"])).strip().lower() == case[\"expected\"][\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n",
    "model": { "name": "<model from GET /models>" }
  },
  "strategy": { "mode": "auto" },
  "budget": { "max_scorer_runs": 200 },
  "reflection_model_config": { "name": "<model from GET /models>" },
  "max_cost_cents": 500
}
```

Campos importantes:

- `target.repo.repository` es el repositorio de GitHub en formato `owner/name`. Primero conecta GitHub en **Ajustes → Conectores**.
- `target.repo.editable_paths` enumera los archivos y carpetas que el optimizador puede cambiar. `"."` significa todo el repositorio.
- `target.repo.commit` es opcional. Es un SHA de 40 caracteres. Sin él, Skynet usa el último commit de la rama.
- `target.repo.secrets` es opcional. Cada secreto tiene un `name` y un `value`. Llega a tu código y a tu evaluador como variable de entorno.
- `scorer.metric_code` define `score(repo_path, case)`. `scorer.model` es el modelo que tu código y tu evaluador pueden llamar a través de la pasarela de modelos.
- `strategy.mode` es `auto` (el valor predeterminado) o `single`. Con `single`, pon en `strategy.engine` el nombre de un motor de `GET /blackbox/engines`.
- `max_cost_cents` es el límite de gasto de esta ejecución, en centavos de dólar estadounidense.
- `is_private` es `false` por defecto. Ponlo en `true` para que la ejecución sea privada.

Lee [Métricas y evaluadores](/docs/concepts/scorers) para conocer las reglas de `metric_code`.

### Respuestas

| Estado | Significado                                                                                                                                                       |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet puso la ejecución en cola. El cuerpo contiene `optimization_id` y `status`.                                                                                 |
| `400`  | El destino no es un repositorio, o el evaluador no se carga.                                                                                                     |
| `202`  | La comprobación de la configuración sigue en curso. Espera los segundos que indica la cabecera `Retry-After`. Después envía el mismo cuerpo con la misma `Idempotency-Key`. |
| `402`  | Tu saldo es demasiado bajo.                                                                                                                                       |
| `409`  | Alcanzaste una cuota, o la comprobación de la configuración está desactualizada.                                                                                 |
| `422`  | La solicitud no encaja con el optimizador.                                                                                    |

> [!NOTE]
> Envía siempre una cabecera `Idempotency-Key`. Si vuelves a enviar la misma solicitud con la misma clave, Skynet devuelve la primera ejecución. No inicia una segunda ejecución.

## Seguir una ejecución

Consulta la ejecución periódicamente hasta que su estado sea final:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

El campo `status` toma uno de estos valores:

| Estado                             | Final |
| ---------------------------------- | ----- |
| `pending`, `validating`, `running` | No    |
| `paused`                           | No    |
| `success`                          | Sí    |
| `failed`, `cancelled`, `stopped`   | Sí    |

Consulta cada 10 a 30 segundos. Una ejecución puede tardar desde minutos hasta horas.

La [referencia de la API](https://api.skynetml.com/scalar) enumera todos los campos de la respuesta de una ejecución.

## Usar el resultado

Cuando el estado es `success`, obtén la mejor versión:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Para ejecutar la mejor versión con una entrada nueva, envía `POST /serve/{optimization_id}` con una cabecera `Idempotency-Key`. El repositorio debe tener un archivo `serve.py` con `def serve(input)`. Lee [Resultados](/docs/guides/results) para conocer la solicitud, la respuesta y qué significa cada puntuación.

## Límites

La API usa las mismas cuotas, límites de frecuencia y facturación que la aplicación web. Lee [Límites](/docs/limits) y [Facturación](/docs/concepts/billing).
