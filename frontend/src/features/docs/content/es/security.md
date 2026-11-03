Esta página describe cómo protege Skynet tu cuenta, tus claves y tus datos. También explica cómo informar de un problema de seguridad.

## Informar de una vulnerabilidad

Informa de los problemas de seguridad en privado. No abras una incidencia pública.

- En GitHub, abre el repositorio de Skynet y ve a **Security** > **Report a vulnerability**.
- O envía un correo a `security@skynetml.com`.

El equipo procura confirmar la recepción de cada informe en un plazo de cinco días hábiles.

## Tu cuenta

### Métodos de inicio de sesión

Puedes iniciar sesión con:

- Una dirección de correo y una contraseña.
- **Continuar con Google** o **Continuar con GitHub**.
- **Iniciar sesión con una passkey**. Una passkey usa Face ID, Touch ID o una llave de hardware.

Puedes gestionar los proveedores que usas en **Cuentas vinculadas**.

### Reglas de contraseña

Una contraseña debe:

- Tener al menos 8 caracteres.
- Tener como máximo 128 caracteres.
- No contener tu dirección de correo.

Una cuenta nueva debe verificar su dirección de correo con un código. Restablecer la contraseña también usa un código que Skynet envía por correo.

Skynet bloquea el inicio de sesión de una dirección de correo durante un tiempo después de varios intentos fallidos.

### Verificación en dos pasos

Activa la verificación en dos pasos para tener más protección. Elige un método:

| Método                                  | Cómo funciona                                                         |
| --------------------------------------- | --------------------------------------------------------------------- |
| **App de autenticación**                | Introduce un código de 6 dígitos de tu app de autenticación.          |
| **Códigos por correo electrónico**      | Introduce un código de 6 dígitos que Skynet envía en cada inicio de sesión. |

Skynet también te da códigos de recuperación. Guárdalos en un lugar seguro. Usa uno si pierdes el acceso a tu método.

> [!NOTE]
> Si inicias sesión con Google o GitHub, ese proveedor gestiona tu verificación en dos pasos.

## Dónde se ejecuta tu código

Las cargas de trabajo de los usuarios se ejecutan en sandboxes aislados. En el producto alojado, Vercel Sandbox es el límite de ejecución. Esto abarca las comprobaciones de configuración, las ejecuciones de optimización, la recuperación y el trabajo sobre ejecuciones completadas.

Los workers de Skynet no ejecutan tu código. Son el plano de control. Inician trabajos, reservan y liquidan presupuestos, entregan credenciales con ámbito limitado a los sandboxes y guardan puntos de control.

Antes de que Skynet inicie trabajo de pago, reserva margen en tu monedero y en el presupuesto de la ejecución. Consulta [Facturación](/docs/concepts/billing).

En la [app para Mac](/docs/guides/mac-app), los trabajos se ejecutan en contenedores Docker locales. Todos los servicios locales escuchan solo en `127.0.0.1`.

## Claves de proveedor (BYOK)

Puedes añadir tus propias claves de proveedores de modelos.

- Skynet cifra cada clave en reposo.
- Skynet nunca devuelve la clave completa a través de la API.
- Skynet solo guarda los cuatro últimos caracteres y un estado: sin verificar, verificada o no válida.
- Cuando introduces una clave, Skynet envía una pequeña solicitud de prueba para comprobarla.

Tu proveedor factura los tokens del modelo a tu clave. Skynet no cobra ninguna tarifa por el modelo. El presupuesto de la ejecución solo cubre el uso del entorno aislado.

Skynet también elimina los secretos de los registros y de los mensajes de error. Los sustituye por `[REDACTED]`.

## Tokens de API

Los tokens de API permiten que scripts y herramientas llamen a la API de Skynet.

- Cada usuario tiene como máximo un token válido. Cuando generas un token nuevo, el anterior deja de funcionar.
- Los tokens empiezan por `skyd_`.
- Skynet solo guarda un hash del token.
- Skynet muestra el token completo una sola vez. Copia y guarda el token en ese momento.
- Después, Skynet solo muestra una forma enmascarada: `skyd_…` y los cuatro últimos caracteres.

Envía el token en la cabecera `Authorization`:

```http
Authorization: Bearer skyd_...
```

Para invalidar un token, haz clic en **Revocar token**. Para conocer la API completa, lee [API](/docs/api).

> [!WARNING]
> Trata el token como una contraseña. Cualquiera que tenga el token puede actuar en tu nombre a través de la API. No lo incluyas en un commit de un repositorio.

## Privacidad de datos de los modelos

El ajuste **Privacidad de datos de los modelos** controla qué pueden hacer los proveedores de modelos con tus datos. Se aplica a las optimizaciones que Skynet cobra a tu saldo.

| Opción                 | Efecto                                                                                           |
| ---------------------- | ------------------------------------------------------------------------------------------------ |
| **Permitir todos**     | Cualquier proveedor, incluidos los que pueden entrenar con tus prompts o guardarlos.             |
| **Sin entrenamiento**  | Solo proveedores que no entrenan con tus prompts y no los conservan. Es la opción predeterminada. |
| **Sin retención**      | Solo endpoints con un acuerdo de retención cero de datos. Algunos modelos pueden no estar disponibles. |

> [!NOTE]
> Las optimizaciones con tu propia clave siguen los ajustes de privacidad de tu cuenta de OpenRouter.

## Visibilidad de las ejecuciones

> [!WARNING]
> Las ejecuciones nuevas son públicas por defecto (**Pública**). Una ejecución pública aparece en la búsqueda pública. Cualquiera puede encontrarla y consultar sus resultados.

Para mantener una ejecución fuera de la búsqueda pública, pon **Visibilidad** en **Privada** al enviarla. Puedes cambiar la visibilidad más tarde en el diálogo de compartir. Consulta [Compartir](/docs/guides/sharing).

Los conjuntos de datos nuevos tienen acceso **Restringido**. Solo tú y las personas que invites pueden abrirlos, hasta que cambies el acceso del enlace. Consulta [Preparar un conjunto de datos](/docs/guides/datasets).

## Tus datos

Abre la pestaña **Privacidad y datos** en **Ajustes** para gestionar tus datos.

### Exportar tus datos

Haz clic en **Exportar** en **Exportar mis datos**. La exportación contiene todos los datos de tu cuenta. Elige un formato: JSON, CSV, Excel, Pickle, Parquet o Feather.

### Eliminar tu cuenta

Haz clic en **Eliminar cuenta**. Introduce tu contraseña actual y escribe tu dirección de correo para confirmar.

> [!WARNING]
> Esto elimina de forma permanente tu cuenta y todos sus datos. No se puede deshacer.

### Analíticas de uso

El ajuste **Enviar analíticas de uso anónimas** activa o desactiva las analíticas anónimas del producto.

## Próximos pasos

- Lee [Límites](/docs/limits).
- Lee las [Preguntas frecuentes](/docs/faq).
