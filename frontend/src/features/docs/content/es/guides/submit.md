El asistente de envío configura e inicia una ejecución. Tiene cuatro pasos: **Tarea**, **Entradas**, **Optimizador** y **Métrica y ejecución**. Cada paso tiene una o más pantallas. Esta guía explica cada pantalla, desde la tarea hasta la comprobación y el inicio de la ejecución.

> [!NOTE]
> El asistente muestra más o menos ajustes según el nivel de experiencia: **Guiado**, **Estándar** o **Experto**. Guiado es el nivel predeterminado. Cambia el nivel en **Ajustes → Cuenta**, en **Cuánto mostrar**. Algunas etiquetas son más sencillas en el nivel Guiado. Esta guía usa las etiquetas de los niveles Estándar y Experto, y da entre paréntesis la etiqueta del nivel Guiado cuando es distinta.

## Paso 1: Tarea

### ¿Qué quieres optimizar?

Describe tu tarea al asistente de IA en el chat **Entrevista sobre la tarea**. El asistente de IA escribe dos campos por ti:

- **Objetivo** (Guiado: **Meta**): lo que el código optimizado debe hacer bien.
- **Contexto** (Guiado: **Contexto adicional**): el contexto, las restricciones y cómo es un buen resultado.

Puedes editar los dos campos. El asistente de IA no sobrescribe tus ediciones.

### De dónde viene el código

Skynet es Optimize Anything: cada ejecución optimiza un repositorio. Selecciona una de dos opciones:

- **Usar un repositorio**: optimiza código de un repositorio de GitHub que conectaste.
- **Crear un repositorio nuevo**: Skynet crea un repositorio de GitHub nuevo en tu cuenta. El asistente de IA escribe una primera solución a partir de tu tarea y tus datos. Esa solución es la versión inicial.

Conecta GitHub en **Ajustes → Conectores** antes de empezar.

### Usar un repositorio

Rellena estos campos:

- **Repositorio**: elige un repositorio, o escríbelo como `owner/name`.
- **Commit**: un SHA de 40 caracteres. Déjalo vacío para usar el último commit de la rama.
- **Rutas que el optimizador puede cambiar**: un archivo o una carpeta por línea. Un punto significa el repositorio completo.
- **Comando de instalación**: se ejecuta en cada copia nueva del repositorio antes de puntuar. Déjalo vacío y Skynet encontrará el comando.

### Crear un repositorio nuevo

Rellena estos campos:

- **Nombre**: el nombre del repositorio nuevo. Tu cuenta de GitHub no debe tener ya un repositorio con este nombre.
- **Privado** o **Público**: quién puede ver el repositorio en GitHub.

Skynet sube cada candidato a su propia rama. La rama `skynet/best` apunta a la mejor versión.

## Paso 2: Entradas

### Datos

Añade los casos de la ejecución. Puedes:

- Subir un archivo CSV, JSON o Excel.
- Elegir un conjunto de datos de tu biblioteca.

Lee [Conjuntos de datos y divisiones](/docs/concepts/datasets) y [Preparar un conjunto de datos](/docs/guides/datasets).

### Cómo leer los datos

El asistente de IA lee los datos y sugiere cómo usarlos. Revisa sus elecciones:

- Marca cada columna como entrada o como salida esperada, u omítela.
- Sin columna de salida, Skynet usa la primera fila como una única entrada.
- Fija la división en los conjuntos de entrenamiento, validación y prueba. La división debe sumar 1.

> [!WARNING]
> No puedes abrir los pasos siguientes mientras el paso **Entradas** tenga un problema. Corrige los problemas que muestra el asistente y luego continúa.

## Paso 3: Optimizador

En el nivel Estándar, este paso tiene una sola pantalla. Muestra el optimizador, el presupuesto y, cuando hace falta, los modelos. En los niveles Guiado y Experto, el paso tiene tres pantallas: **Optimizador**, **Modelos** y **Presupuesto**.

### Optimizador

El asistente solo muestra los optimizadores que encajan con tu tarea y tus entradas. **Auto** está seleccionado por ti. Selecciona otro optimizador si quieres. Lee [Motores](/docs/concepts/engines) y [Cómo funciona la optimización](/docs/concepts/optimization).

### Modelos

- **Modelo de reflexión**: el modelo que lee las puntuaciones y los comentarios y escribe los siguientes candidatos. Con un optimizador de agente de código, es el modelo del agente.
- **Modelo de tarea**: el modelo al que pueden llamar el código de tu repositorio y tu evaluador a través de la pasarela de modelos de Skynet. Lee [Cómo funciona la optimización](/docs/concepts/optimization).

### Presupuesto

Fija cuánto tiempo puede seguir intentándolo la ejecución, y cuánto puede gastar:

- **Máximo de ejecuciones del evaluador** (Guiado: **Intentos máx.**): el número máximo de candidatos que puntúa Skynet. Algunos optimizadores necesitan un número mínimo.
- **Detener al llegar a la puntuación** (Guiado: **Detener antes cuando la puntuación llegue a**): la ejecución se detiene cuando una versión alcanza esta puntuación.
- **Iteraciones máx.** (Guiado: **Rondas máx.**): aparece para los optimizadores que lo admiten.
- **Límite de gasto**: lo máximo que puede gastar la ejecución. El valor predeterminado es 5 $, o el importe **Necesario para empezar** si es mayor. También puedes seleccionar **Sin límite**.

El asistente no te deja continuar si el límite o tu saldo es inferior al importe necesario para empezar. Lee [Presupuestos y facturación](/docs/concepts/billing).

## Paso 4: Métrica y ejecución

### Evaluador

Escribe `scorer.py`. Define `score(repo_path, case)` y devuelve una puntuación con comentarios. El asistente de IA puede escribirlo por ti. Lee [Métricas y evaluadores](/docs/concepts/scorers).

El repositorio puede tener un archivo `serve.py` con `def serve(input)`. Skynet lo usa para servir la mejor versión después de la ejecución. Lee [Leer los resultados](/docs/guides/results).

### Secretos

Añade secretos, como las claves de API que necesita tu código. Cada secreto llega a tu código y a tu evaluador como una variable de entorno con el mismo nombre. El agente de código no puede verlos. Skynet oculta sus valores en los registros.

### Comprobación

La comprobación (Guiado: **Ejecución de prueba**) ejecuta la instalación y tu evaluador sobre la versión inicial en el sandbox. Comprueba:

- la firma del evaluador,
- el comando de instalación,
- el valor que devuelve el evaluador sobre los casos de muestra.

Selecciona **Comprobar** (Guiado: **Probar**). No puedes iniciar la ejecución hasta que la comprobación sea correcta. Si cambias el evaluador o la configuración después de una comprobación correcta, vuelve a ejecutar la comprobación.

> [!TIP]
> Abre cada muestra para ver su puntuación y sus comentarios. Unos buenos comentarios dan al optimizador más información con la que trabajar que la puntuación sola.

### Ejecución programada

Puedes volver a ejecutar la optimización de forma programada después de que termine correctamente. Cada ciclo programado empieza desde la mejor versión actual del repositorio. El valor predeterminado es **Desactivada**.

### Listo para ejecutar

La última pantalla muestra un resumen en cinco pestañas: **General**, **Datos**, **Modelos**, **Presupuesto** y **Recompensa**. Revisa la configuración y luego inicia la optimización.

El resumen también muestra la visibilidad de la ejecución. Las ejecuciones nuevas son públicas por defecto. Cámbiala más tarde en el cuadro de diálogo **Compartir y privacidad** de la página de la ejecución. Lee [Compartir y publicar](/docs/guides/sharing).

## Después de iniciar

Se abre la página de la ejecución. Lee [Ejecuciones](/docs/concepts/runs) y [Leer los resultados](/docs/guides/results).
