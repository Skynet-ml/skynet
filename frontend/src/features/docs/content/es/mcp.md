El servidor MCP de Skynet permite que un agente de IA use Skynet por ti. Claude, ChatGPT, Codex, Cursor y otros clientes MCP pueden comprobar la configuración de una ejecución, iniciar ejecuciones, seguirlas y detenerlas. Apruebas la conexión una sola vez en tu navegador. El agente fija un límite de gasto para cada ejecución.

## URL del servidor

Todos los clientes usan la misma URL:

```text
https://mcp.skynetml.com/mcp
```

El servidor usa Streamable HTTP. El inicio de sesión usa OAuth 2.1. No copias ningún token ni ninguna clave de API en el cliente.

## Conecta tu cliente

Selecciona tu cliente. Sigue sus pasos y luego ve a [Aprobar la conexión](#aprobar-la-conexión).

```skynet-mcp-clients

```

## Aprobar la conexión

La primera vez que el agente llama a Skynet, tu navegador abre una página de Skynet.

1. Inicia sesión en Skynet, si aún no lo has hecho.
2. Lee la página. Muestra el nombre del agente, el sitio al que vuelve y tu cuenta.
3. Lee lo que puede hacer el agente:
   - Ver tus ejecuciones de optimización y sus resultados.
   - Iniciar y detener ejecuciones de optimización, pagadas con tu saldo.
4. Haz clic en **Allow** para conectar. Haz clic en **Deny** para cancelar.

Cuando haces clic en **Allow**, el navegador vuelve al agente. A partir de ese momento, el agente puede llamar a las herramientas que siguen.

> [!NOTE]
> El agente fija un límite de gasto para cada ejecución. No puede añadir fondos.

## Herramientas

| Herramienta | Qué hace                                                                                         | Acceso    |
| ----------- | ------------------------------------------------------------------------------------------------ | --------- |
| `check_run` | Comprueba la configuración de una ejecución sin ejecutarla ni cobrarte. Sugiere un límite de gasto. | Lectura   |
| `start_run` | Inicia una ejecución con un límite de gasto.                                                     | Escritura |
| `get_run`   | Devuelve el estado, el gasto y, cuando la ejecución termina, el resultado.                       | Lectura   |
| `list_runs` | Enumera tus ejecuciones, de la más reciente a la más antigua. Puede filtrar por estado.          | Lectura   |
| `stop_run`  | Detiene una ejecución. Una segunda llamada devuelve un error que indica que la ejecución ya terminó. | Escritura |

El acceso de lectura usa el ámbito `runs:read`. El acceso de escritura usa el ámbito `runs:write`.

Ninguna herramienta añade fondos, cambia tu saldo ni cambia los ajustes de facturación.

## Cómo ejecuta un agente una optimización

1. El agente llama a `check_run` con la configuración de la ejecución. Skynet realiza las comprobaciones estáticas y devuelve:
   - El resultado de cada comprobación.
   - El límite de gasto más bajo con el que puede empezar la ejecución.
   - Un límite de gasto sugerido. Es de al menos 5,00 $.
   - Los modelos que no tienen un precio conocido.
   - La cantidad que puedes gastar ahora.
2. El agente llama a `start_run` con un límite de gasto (`max_cost_cents`, de 1 a 1.000.000 centavos de dólar estadounidense). También puede enviar una `idempotency_key`, para que un reintento no inicie una segunda ejecución.
3. Si la comprobación de la configuración sigue en curso, `start_run` devuelve `in_progress` y `retry_after_seconds`. Pasado ese tiempo, el agente vuelve a llamar a `start_run` con la misma entrada.
4. El agente llama a `get_run` hasta que la ejecución termina. Los estados finales son `success`, `failed`, `cancelled` y `stopped`.

> [!TIP]
> Indica tu límite de gasto al agente en tu prompt, por ejemplo: «Optimiza este repositorio. Gasta como máximo 10 $». Así el agente usa ese límite en `start_run`.

La ejecución se detiene cuando alcanza su límite de gasto. Lee [Facturación](/docs/concepts/billing) para saber cómo cobra Skynet una ejecución.

## Seguridad

- Cada cliente recibe su propia autorización. El cliente nunca ve tu contraseña.
- El inicio de sesión usa PKCE (S256). El cliente se registra mediante el registro dinámico de clientes o un documento de metadatos de ID de cliente.
- Los tokens de acceso caducan al cabo de 1 hora. El cliente los renueva con un token de actualización.
- Un token de actualización caduca tras 30 días sin uso. Una autorización dura como máximo 90 días. Después vuelves a aprobar la conexión.
- Cada token de actualización funciona una sola vez. Si alguien vuelve a usar un token de actualización antiguo, Skynet revoca la autorización.
- El agente solo puede ver y cambiar tus propias ejecuciones.

Lee [Seguridad](/docs/security) para saber cómo protege Skynet tu cuenta y tus datos.

## Desconectar

Para desconectar un agente, quita el servidor de Skynet del cliente. La mayoría de los clientes revocan entonces sus tokens. Si un cliente no los revoca, su token de actualización caduca tras 30 días sin uso.

## Solución de problemas

| Problema                                          | Qué hacer                                                                    |
| ------------------------------------------------- | ---------------------------------------------------------------------------- |
| La conexión falla de inmediato                    | Revisa la URL del servidor. Debe ser exactamente `https://mcp.skynetml.com/mcp`. |
| «This agent can't connect»                        | Vuelve a iniciar la conexión desde el cliente.                               |
| «Too many attempts»                               | Espera unos minutos y vuelve a intentarlo.                                   |
| El cliente no abre un navegador                   | Actualiza el cliente. Debe admitir servidores MCP remotos con OAuth.         |
| `start_run` falla con un error de saldo           | Añade fondos en Skynet, o inicia la ejecución con un límite de gasto más bajo. |
| Las herramientas dejan de funcionar tras unas semanas | La autorización caducó. Quita el servidor y vuelve a conectarlo.         |

Para acceder directamente por HTTP sin un agente, lee [API](/docs/api).
