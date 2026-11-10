La app de Skynet para Mac ejecuta Skynet en tu propio ordenador. Envía las llamadas a los modelos a través de una CLI de programación con la sesión iniciada en tu Mac, de modo que el plan de tu CLI paga esas llamadas. También puedes vincular la app a tu cuenta alojada de Skynet.

## Requisitos

- Un Mac con Apple silicon. Existen versiones para Intel, pero nadie las ha probado en hardware Intel.
- Docker Desktop, en ejecución, con **Use Rosetta for x86_64/amd64 emulation** activado.
- Una CLI de programación, con la sesión iniciada:
  - Claude Code (`claude`) 2.1.226 o posterior, o
  - Codex (`codex`) 0.160.0 o posterior.

> [!WARNING]
> Codex debe usar un inicio de sesión de ChatGPT. Un inicio de sesión con una clave de API no sirve. Un inicio de sesión guardado en el Llavero de macOS tampoco sirve.

### Configurar Codex

Codex debe guardar sus credenciales en un archivo. Sigue estos pasos antes del primer inicio:

1. Abre `~/.codex/config.toml` y añade esta línea:

```toml
cli_auth_credentials_store = "file"
```

2. Inicia sesión con tu cuenta de ChatGPT:

```bash
codex login
```

## Instalar la app

Las versiones tienen una firma ad hoc y no están notarizadas. macOS las bloquea hasta que quitas el atributo de cuarentena.

1. Abre el archivo `.dmg`.
2. Arrastra **Skynet** a **Aplicaciones**.
3. Ejecuta este comando en Terminal:

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. Abre Skynet desde **Aplicaciones**.

> [!NOTE]
> El primer inicio tarda varios minutos. La app construye una imagen de sandbox local y prepara su base de datos.

## Qué ocurre al iniciar

La app sigue estos pasos en orden:

1. Lee el `PATH` de tu shell de inicio de sesión, para poder encontrar Docker y tu CLI.
2. Comprueba Docker y tu CLI de programación.
3. Inicia una base de datos Postgres local en Docker.
4. Construye la imagen de sandbox `skynet-local-sandbox` para `linux/amd64`. Docker la ejecuta con Rosetta.
5. Actualiza el esquema de la base de datos.
6. Inicia la API local y la app web, y muestra la app web en la ventana.

### Comprobaciones iniciales

La app muestra una lista de comprobaciones: **Docker**, **Coding CLI**, **Signed in**, **Usage limit**, **Local services** y **Account**. Si una comprobación falla, la app muestra una pantalla que te indica qué hacer:

| Pantalla                                   | Qué hacer                                                                          |
| ------------------------------------------ | ---------------------------------------------------------------------------------- |
| **Install Docker Desktop**                 | Instala Docker Desktop.                                                            |
| **Start Docker**                          | Inicia Docker Desktop.                                                             |
| **Install a coding CLI**        | Instala Claude Code o Codex.                                                       |
| **Update your coding CLI**       | Actualiza la CLI a la versión mínima indicada arriba.                              |
| **Sign in to your CLI**                | Inicia sesión en la CLI desde Terminal.                                            |
| **Usage limit reached**                | Espera a la hora de restablecimiento. Los trabajos nuevos pueden empezar después.  |

Haz clic en **Check again** después de solucionar el problema. Haz clic en **Open logs** para ver los archivos de registro. En la pantalla **Usage limit reached**, puedes hacer clic en **Open Skynet anyway**.

## Ejecuciones locales

En la app, las llamadas a los modelos pasan por la CLI de tu Mac. El menú de modelos muestra este modo como **Local**. La indicación dice: «Tu plan paga las llamadas, así que este modelo añade 0 $ a la ejecución.»

Los nombres de las CLI en la app son **Claude Code** y **Codex**.

> [!NOTE]
> La app quita las claves de API del entorno de los procesos que inicia. Así, la CLI usa tu suscripción y no una clave de API.

### Ejecutar el proponente en tu CLI

El ajuste **Ejecutar el proponente en mi CLI** controla dónde se ejecuta el agente proponente. Las opciones son **Motor del agente (predeterminado)** y **Desactivado (proponente aislado)**.

**Acceso del proponente** fija a qué puede acceder la CLI:

- **Solo el espacio de trabajo** es la opción predeterminada. Se apoya en el sandbox propio de cada CLI.
- **Acceso total** ejecuta la CLI con tus privilegios de usuario.

> [!WARNING]
> Con **Acceso total**, la CLI puede leer, cambiar y eliminar cualquier archivo al que tú puedas acceder, y conectarse a la red. El modo acceptEdits de Claude no aísla la red, ni siquiera con **Solo el espacio de trabajo**.

### Etiquetado en la app

El etiquetado con IA en la app para Mac necesita tu propia clave del proveedor. Elige un modelo en **Tu propia clave**. Consulta [Etiquetar datos con el etiquetador](/docs/guides/tagger).

## Vincular tu cuenta de Skynet

Puedes usar la app sin una cuenta. Para ello, haz clic en **Use without signing in**. Para sincronizar con tu cuenta alojada, vincula la app.

### Vincular este Mac

1. En el paso **Sign in to Skynet**, la app abre la página **Link a Mac** en tu navegador predeterminado.
2. Inicia sesión en Skynet en el navegador si te lo pide.
3. Revisa la lista bajo **Una vez vinculado, este Mac podrá:**.
4. Haz clic en **Vincular este Mac**. La página muestra «Volviendo a la app de Mac…».

Si el inicio de sesión falla o lo cancelas, la app muestra **Try again** y **Use without signing in**.

Una vez vinculado, el Mac puede:

- Ver tu nombre y tu correo electrónico.
- Ver tu saldo.
- Sincronizar tus ejecuciones con tu cuenta.
- Usar modelos alojados, que se pagan con tu saldo.
- Usar tus fuentes de datos conectadas.
- Buscar y navegar por la web a través de Skynet.

El vínculo usa un flujo de inicio de sesión seguro (OAuth con PKCE). La app guarda sus tokens cifrados en tu Mac. Cuando desvinculas el Mac, Skynet revoca los tokens.

### Límites del vínculo

| Límite                                | Valor                                         |
| ------------------------------------- | --------------------------------------------- |
| Macs vinculados por cuenta            | 10                                            |
| Duración del token de acceso          | 1 hora; después la app lo renueva             |
| Caducidad del vínculo por inactividad | 30 días sin uso                               |
| Antigüedad máxima del vínculo         | 365 días                                      |

Después de la caducidad por inactividad o de la antigüedad máxima, vuelve a vincular el Mac.

### Estado del vínculo

La app muestra uno de estos estados: **Modo local**, **Vinculado a este Mac**, **Inicia sesión para sincronizar** o **Vinculado (sin conexión)**.

## Servicios y datos locales

Todos los servicios locales escuchan solo en `127.0.0.1`:

| Servicio | Rango de puertos |
| -------- | ---------------- |
| API      | 48100 a 48119    |
| App web  | 48120 a 48139    |
| Postgres | 48140 a 48159    |

La app guarda sus datos en `~/Library/Application Support/Skynet`. La carpeta `logs/` contiene `desktop.log`, `api.log`, `frontend.log`, `migrate.log` y `sandbox-build.log`.

Cuando cierras la app, esta detiene la app web, la API, Postgres y todos los contenedores de trabajos.

### Restablecer la app

> [!WARNING]
> Estos comandos eliminan todos los datos locales, incluidas tus ejecuciones y tus conjuntos de datos locales.

Primero cierra la app. Después ejecuta:

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## Próximos pasos

- [Iniciar una ejecución con el asistente](/docs/guides/submit).
- Lee sobre la [facturación](/docs/concepts/billing) de los modelos alojados.
- Lee [Seguridad](/docs/security).
