Esta página responde preguntas frecuentes sobre Skynet. Cada respuesta enlaza a la página con todos los detalles.

## General

### ¿Qué es Skynet?

Skynet es una plataforma que optimiza prompts, programas, agentes y flujos de trabajo con tus propios datos y tu propia métrica. Subes un conjunto de datos, describes la tarea e inicias una ejecución. La ejecución prueba versiones nuevas y las puntúa con datos reservados. Para empezar, lee el [Inicio rápido](/docs/quickstart).

### ¿Qué puede optimizar Skynet?

Skynet es Optimize Anything. Optimiza prompts, código, texto y programas DSPy en un solo producto. El tipo de artefacto decide qué motores pueden trabajar con él:

- Los artefactos de tipo Prompt, Código y Texto usan los motores nativos: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler y ShinkaEvolve.
- Los programas DSPy usan los optimizadores de DSPy: DSPy GEPA, DSPy MIPROv2 y DSPy BootstrapFewShot.

**Auto** combina los motores que encajan con tu tarea. Consulta [Cómo funciona la optimización](/docs/concepts/optimization) y [Motores](/docs/concepts/engines).

### ¿Qué idiomas admite la interfaz?

La interfaz está disponible en 24 idiomas. El hebreo es el idioma base, y el diseño admite texto de derecha a izquierda. La documentación en hebreo está en [/docs/he](/docs/he).

### ¿Skynet es de código abierto?

Sí. El código usa la licencia AGPL-3.0. Si ofreces como servicio una versión modificada de Skynet, la licencia te obliga a compartir tus cambios.

## Datos

### ¿Qué tipos de archivo puedo subir?

Puedes subir archivos CSV, JSON, `.xlsx` y `.xls`. En los archivos de Excel, Skynet solo lee la primera hoja. Consulta [Preparar un conjunto de datos](/docs/guides/datasets).

### Mis datos no tienen etiquetas. ¿Qué puedo hacer?

Usa el etiquetador. Puedes etiquetar las filas tú mismo, o dejar que un modelo de IA las etiquete mientras revisas su trabajo. Consulta [Etiquetar datos con el etiquetador](/docs/guides/tagger).

### ¿Quién puede ver mis ejecuciones y mis conjuntos de datos?

> [!WARNING]
> Las ejecuciones nuevas son públicas por defecto (**Pública**). Una ejecución pública aparece en la búsqueda pública, y cualquiera puede consultar sus resultados.

Pon **Visibilidad** en **Privada** al enviar una ejecución para mantenerla fuera de la búsqueda pública. Los conjuntos de datos nuevos tienen acceso **Restringido**: solo tú y las personas que invites pueden abrirlos. Consulta [Compartir](/docs/guides/sharing) y [Seguridad](/docs/security).

### ¿Los proveedores de modelos entrenan con mis prompts?

Por defecto, no. La opción predeterminada de **Privacidad de datos de los modelos** es **Sin entrenamiento**. Solo envía prompts a proveedores que no entrenan con ellos y no los conservan. Este ajuste se aplica a las llamadas que Skynet cobra a tu saldo. Las llamadas con tu propia clave siguen los ajustes de privacidad de tu cuenta de OpenRouter. Consulta [Seguridad](/docs/security).

## Facturación

### ¿Cómo cobra Skynet las ejecuciones?

Skynet usa un saldo prepago. Se mide el uso de modelos alojados, la tarifa de recarga y el uso del sandbox. Antes de que empiece el trabajo de pago, Skynet reserva margen en tu monedero y en el presupuesto de la ejecución. Consulta [Facturación](/docs/concepts/billing).

### ¿Puedo usar mi propia clave de un proveedor de modelos?

Sí. Con tu propia clave (BYOK), Tu proveedor factura los tokens del modelo a tu clave. Skynet no cobra ninguna tarifa por el modelo. El presupuesto de la ejecución solo cubre el uso del entorno aislado. Skynet cifra tu clave y no vuelve a mostrarla nunca. Consulta [Facturación](/docs/concepts/billing) y [Seguridad](/docs/security).

### ¿Puedo usar Skynet sin pagar por las llamadas al modelo?

La [app para Mac](/docs/guides/mac-app) ejecuta Skynet en tu propio ordenador. Envía las llamadas al modelo a través de Claude Code o Codex, con la sesión iniciada en tu Mac. Tu plan de la CLI paga esas llamadas, así que suman 0 $ a la ejecución.

## Herramientas e integraciones

### ¿Puedo controlar Skynet desde mi propio cliente de IA?

Sí. Skynet tiene un servidor MCP en `https://mcp.skynetml.com/mcp`. Inicias sesión con OAuth. El servidor tiene estas herramientas: `check_run`, `start_run`, `get_run`, `list_runs` y `stop_run`. Consulta [MCP](/docs/mcp).

### ¿Hay una API?

Sí. Genera un token de API en Ajustes y envíalo como `Authorization: Bearer <token>`. Cada usuario tiene un solo token válido a la vez. Consulta [API](/docs/api).

### ¿Puede el panel del agente eliminar mis ejecuciones?

Solo si lo permites. En el modo predeterminado **Preguntar**, el agente pregunta antes de cada acción. En el modo **Automático**, sigue preguntando antes de las acciones destructivas, como eliminar o enviar. Consulta [Panel del agente](/docs/guides/agent-panel).
