El asistente de envío configura e inicia una ejecución. Tiene cuatro pasos: **Tarea**, **Entradas**, **Optimizador** y **Métrica y ejecución**. Cada paso tiene una o más pantallas. Esta guía explica cada pantalla, desde la tarea hasta la comprobación del contrato y el inicio de la ejecución.

> [!NOTE]
> El asistente muestra más o menos ajustes según el nivel de experiencia: **Guiado**, **Estándar** o **Experto**. Guiado es el nivel predeterminado. Cambia el nivel en **Ajustes → Cuenta**, en **Cuánto mostrar**. Algunas etiquetas son más sencillas en el nivel Guiado. Esta guía usa las etiquetas de los niveles Estándar y Experto, y da entre paréntesis la etiqueta del nivel Guiado cuando es distinta.

## Paso 1: Tarea

### ¿Qué quieres optimizar?

Describe tu tarea al asistente de IA en el chat **Entrevista sobre la tarea**. El asistente de IA escribe dos campos por ti:

- **Objetivo** (Guiado: **Meta**): lo que el artefacto optimizado debe hacer bien.
- **Contexto** (Guiado: **Contexto adicional**): el contexto, las restricciones y cómo es un buen resultado.

Puedes editar los dos campos. El asistente de IA no sobrescribe tus ediciones.

### Qué se optimizará

En **Qué se optimiza**, selecciona **Programa DSPy** o **Un repositorio de GitHub**.

Después da la **Versión inicial** (Guiado: **Desde dónde empezamos**). Pega la versión actual, o deja que el asistente de IA escriba un borrador.

### Optimizar un repositorio

También puedes optimizar código de un repositorio. Rellena estos campos:

- **Repositorio**: elige un repositorio, o escríbelo como `owner/name`.
- **Commit**: un SHA de 40 caracteres. Déjalo vacío para usar el último commit de la rama.
- **Rutas que el optimizador puede cambiar**: un archivo o una carpeta por línea. Un punto significa el repositorio completo.
- **Comando de instalación**: se ejecuta en cada copia nueva del repositorio antes de puntuar. Déjalo vacío y Skynet encontrará el comando.

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

- **Modelo de tarea**: el modelo al que llama tu código de ejecución. Déjalo vacío si tu código de ejecución no llama a un modelo. Un programa DSPy debe tener un modelo de tarea.
- **Modelo de reflexión**: el modelo que lee las puntuaciones y los comentarios y escribe los siguientes candidatos. Con un optimizador de agente de código, es el modelo del agente.

### Presupuesto

Fija cuánto tiempo puede seguir intentándolo la ejecución, y cuánto puede gastar:

- **Máximo de ejecuciones del evaluador** (Guiado: **Intentos máx.**): el número máximo de candidatos que puntúa Skynet. Algunos optimizadores necesitan un número mínimo.
- **Detener al llegar a la puntuación** (Guiado: **Detener antes cuando la puntuación llegue a**): la ejecución se detiene cuando una versión alcanza esta puntuación.
- **Iteraciones máx.** (Guiado: **Rondas máx.**): aparece para los optimizadores que lo admiten.
- **Límite de gasto**: lo máximo que puede gastar la ejecución. El valor predeterminado es 5 $, o el importe **Necesario para empezar** si es mayor. También puedes seleccionar **Sin límite**.

El asistente no te deja continuar si el límite o tu saldo es inferior al importe necesario para empezar. Lee [Presupuestos y facturación](/docs/concepts/billing).

## Paso 4: Métrica y ejecución

### Código y métrica

Escribe dos archivos:

- `run.py`: el código de ejecución que ejecuta tu artefacto sobre un caso.
- `metric.py`: la métrica que puntúa la salida y escribe los comentarios.

El **Agente de código** puede escribir y cambiar cualquier archivo por ti. Comprueba su código y corrige lo que falla. Puedes añadir hasta 20 archivos adicionales. Lee [Métricas y evaluadores](/docs/concepts/scorers).

La métrica define su modelo en su propia llamada a `llm()`. Sin `model=`, la métrica usa el modelo de reflexión.

### Comprobación del contrato

La **Comprobación del contrato** (Guiado: **Ejecución de prueba**) ejecuta tu código y tu métrica en el sandbox sobre 3 casos de muestra. Comprueba:

- la firma del código de ejecución,
- la firma de la métrica,
- el modelo de la métrica,
- el código de ejecución sobre las muestras,
- el valor que devuelve la métrica.

Selecciona **Comprobar** (Guiado: **Probar**). No puedes iniciar la ejecución hasta que la comprobación sea correcta. Si cambias el código o la configuración después de una comprobación correcta, vuelve a ejecutar la comprobación.

> [!TIP]
> Abre cada muestra para ver su entrada, su salida y sus comentarios. Unos buenos comentarios dan al optimizador más información con la que trabajar que la puntuación sola.

### Ejecución programada

Puedes volver a ejecutar la optimización de forma programada después de que termine correctamente. El valor predeterminado es **Desactivada**.

### Listo para ejecutar

La última pantalla muestra un resumen en cinco pestañas: **General**, **Datos**, **Modelos**, **Presupuesto** y **Recompensa**. Revisa la configuración y luego inicia la optimización.

El resumen también muestra la visibilidad de la ejecución. Las ejecuciones nuevas son públicas por defecto. Cámbiala más tarde en el cuadro de diálogo **Compartir y privacidad** de la página de la ejecución. Lee [Compartir y publicar](/docs/guides/sharing).

> [!NOTE]
> Una ejecución de repositorio solo tiene en este paso la pantalla **Código y métrica** y el resumen. Skynet comprueba la configuración con el repositorio cuando inicias la ejecución.

## Después de iniciar

Se abre la página de la ejecución. Lee [Ejecuciones](/docs/concepts/runs) y [Leer los resultados](/docs/guides/results).
