El panel del agente es un asistente de chat dentro de Skynet. Puede leer tus ejecuciones, tus conjuntos de datos y tu saldo, y puede actuar por ti, por ejemplo cambiar el nombre de una ejecución o cancelarla. Tú decides qué acciones necesitan tu aprobación.

## Abrir el panel

El panel se abre en un lateral de la app. Cuando lo minimizas, queda en la pantalla una pastilla con la etiqueta **Abrir el agente**. Haz clic en la pastilla para volver a abrir el panel.

Haz clic en **Lo que puedo hacer** para ver la lista de herramientas del agente.

### Conversaciones

- **Nueva conversación** inicia un chat vacío.
- **Conversaciones anteriores** muestra tus chats antiguos. Puedes buscarlos, fijarlos, cambiarles el nombre y eliminarlos.

### Enviar un mensaje

Escribe tu mensaje y pulsa Intro. También puedes hacer clic en **Dictar** y decir tu mensaje en voz alta.

Si el agente está ocupado, tu mensaje espera en **Mensajes pendientes**. Puedes editar, eliminar o reordenar un mensaje pendiente. Haz clic en **Orientar** para enviarlo al trabajo en curso. El agente lo lee en su siguiente paso.

## Lo que puede hacer el agente

El agente puede leer:

- Tus ejecuciones, sus registros y sus resultados de prueba.
- Las analíticas y el saldo de tu monedero.
- Los conjuntos de datos de tu biblioteca y los resultados de búsqueda públicos.
- Tus sesiones de etiquetado (solo lectura).
- Los conjuntos de datos de ejemplo, el catálogo de motores y el catálogo de optimizadores.

El agente también puede actuar. Las acciones seguras incluyen:

- Cambiar el nombre de las ejecuciones y fijarlas, y fijar muchas ejecuciones a la vez.
- Pausar una ejecución.
- Editar y validar el código de una ejecución.
- Analizar un conjunto de datos y fijar sus roles de columna.
- Buscar los modelos disponibles.
- Actualizar el asistente de envío y tus preferencias.
- Preparar un conjunto de datos de ejemplo.
- Crear una carpeta.

Las acciones destructivas incluyen:

- Eliminar una ejecución o muchas ejecuciones.
- Enviar, cancelar, clonar, reintentar, reanudar o reiniciar una ejecución, y cancelar muchas ejecuciones.
- Eliminar, mover o actualizar una carpeta, y mover ejecuciones entre carpetas.

> [!NOTE]
> Las herramientas web (buscar, abrir una URL, rastrear un sitio) solo están disponibles cuando el servidor tiene configurada una clave de búsqueda web. Si no las ves, tu instancia de Skynet no las ofrece.

## Modos de confianza

El modo de confianza determina qué acciones necesitan tu aprobación. Haz clic en el selector de confianza del editor de mensajes para cambiarlo. El selector pasa por los tres modos en orden.

| Modo                   | Descripción                                          | Acciones seguras      | Acciones destructivas |
| ---------------------- | ---------------------------------------------------- | --------------------- | --------------------- |
| **Preguntar**          | Pide confirmación antes de cada acción.              | Necesitan aprobación  | Necesitan aprobación  |
| **Automático**         | Realiza las acciones seguras sin confirmación.       | Se ejecutan al momento | Necesitan aprobación |
| **Sin restricciones**  | Lo realiza todo sin confirmación.                    | Se ejecutan al momento | Se ejecutan al momento |

El modo predeterminado es **Preguntar**. Las herramientas de solo lectura nunca necesitan aprobación.

> [!WARNING]
> En el modo **Sin restricciones**, el agente puede eliminar ejecuciones o enviar ejecuciones de pago sin aprobación. Usa este modo solo cuando confíes en la tarea.

### Fijar el modo predeterminado

1. Abre **Ajustes**.
2. Abre la pestaña **Panel del agente**.
3. En **Nivel de confianza del agente**, elige **Preguntar primero**, **Automático solo para acciones seguras** o **Sin aprobación previa**.

### Aprobar una acción

Cuando una acción necesita aprobación, el panel muestra una tarjeta de aprobación. La tarjeta tiene un botón de confirmación para la acción y **Cancelar**. Haz clic en **Mostrar detalles técnicos** para ver la llamada exacta a la herramienta.

Si no respondes en 15 minutos, el agente considera que rechazaste la acción.

## Adjuntar un archivo

Puedes adjuntar archivos a un mensaje. Cada archivo puede ocupar hasta 20 MB. Puedes adjuntar un conjunto de datos cada vez.

Para subir un conjunto de datos a través del agente:

1. Adjunta un archivo CSV, JSON o Excel.
2. Revisa la tarjeta de subida. Muestra las columnas del archivo.
3. Fija los roles de columna en la tarjeta y luego confirma.

Si el agente no puede leer el archivo, la tarjeta muestra «No se pudo leer el archivo. Usa un archivo CSV, JSON o XLSX.»

Para conocer las reglas de los archivos, consulta [Preparar un conjunto de datos](/docs/guides/datasets).

## Elegir un modelo

El menú de modelos del editor de mensajes tiene estos grupos:

- **Auto** deja que Skynet elija el modelo.
- **Créditos de Skynet** usa un modelo alojado, que se paga con tu saldo.
- **Usa tu propia clave** usa un modelo a través de tu propia clave del proveedor.

Algunos modelos te permiten fijar el esfuerzo de razonamiento. Los niveles son **Ninguno**, **Mínimo**, **Bajo**, **Medio**, **Alto**, **Muy alto** y **Máximo**. **Predeterminado** usa el nivel predeterminado del modelo.

Para fijar un modelo predeterminado para los chats nuevos, abre **Ajustes** > **Panel del agente** > **Modelo predeterminado**.

### Coste

Los chats con el agente que usan créditos de Skynet consumen tu saldo. Si tu saldo está vacío, el panel muestra «Tu saldo está vacío. Añade fondos para seguir chateando con el agente.» Para conocer los precios, lee [Presupuestos y facturación](/docs/concepts/billing).

## Consejos

> [!TIP]
> Pide al agente que te explique una ejecución antes de cambiarla. Por ejemplo: «¿Por qué la ejecución 42 puntuó menos que la ejecución 41?»

> [!TIP]
> Usa el modo **Preguntar** cuando pruebes un tipo nuevo de solicitud. Cambia a **Automático** cuando sepas lo que hará el agente.

## Otras formas de usar un agente

También puedes conectar tu propio cliente de IA a Skynet a través del servidor MCP. Consulta [Servidor MCP](/docs/mcp). Para scripts, usa la [API](/docs/api).

## Próximos pasos

- [Iniciar una ejecución con el asistente](/docs/guides/submit).
- [Leer los resultados](/docs/guides/results).
- Lee [Seguridad](/docs/security) para saber cómo protege Skynet tus claves.
