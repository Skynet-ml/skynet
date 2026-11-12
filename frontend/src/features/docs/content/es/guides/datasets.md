Un conjunto de datos es una tabla de ejemplos de la que aprende una ejecución de optimización y con la que se puntúa. Esta guía explica cómo subir un archivo, editar filas y columnas, fijar los roles de columna y compartir un conjunto de datos. Para los conceptos, lee [Conjuntos de datos y divisiones](/docs/concepts/datasets).

## Archivos compatibles

Skynet lee estos tipos de archivo:

| Extensión       | Cómo lo lee Skynet                                                                          |
| --------------- | ------------------------------------------------------------------------------------------- |
| `.csv`          | La primera línea es la fila de encabezado. El archivo necesita al menos una fila de datos. |
| `.json`         | Un array de objetos, o un solo objeto. Las claves del primer objeto pasan a ser las columnas. |
| `.xlsx`, `.xls` | Skynet solo lee la primera hoja.                                                            |

Skynet rechaza otras extensiones con el mensaje «Unsupported file format».

> [!TIP]
> Pon la salida esperada en la última columna. El asistente de envío marca por defecto la última columna como la salida.

### CSV de ejemplo

```csv
question,answer
What is the capital of France?,Paris
How many legs does a spider have?,8
```

### JSON de ejemplo

```json
[
  { "question": "What is the capital of France?", "answer": "Paris" },
  { "question": "How many legs does a spider have?", "answer": "8" }
]
```

## Subir un conjunto de datos

1. Abre **Conjuntos de datos**.
2. Haz clic en **Subir conjunto de datos**, o arrastra un archivo a la página. Mientras arrastras, la página muestra «Suelta para subir».
3. Espera el aviso «Conjunto de datos guardado en tu biblioteca».

Skynet usa el nombre del archivo sin su extensión como nombre del conjunto de datos. Para cambiarlo, selecciona el conjunto de datos y haz clic en **Cambiar nombre**.

> [!NOTE]
> Cada cuenta tiene una cuota de almacenamiento. Si una subida supera la cuota, Skynet abre un cuadro de diálogo que explica el límite. Consulta [Límites](/docs/limits).

También puedes subir un archivo en el asistente de envío («Sube un archivo CSV, JSON o Excel») o adjuntarlo en el [panel del agente](/docs/guides/agent-panel).

## Buscar un conjunto de datos

La biblioteca muestra tus conjuntos de datos. Usa **Buscar por nombre** para filtrar la lista. Los conjuntos de datos que otras personas comparten contigo aparecen en **Compartido conmigo**.

Cuando seleccionas un conjunto de datos, aparecen estas acciones:

- **Etiquetar este conjunto de datos** abre el [etiquetador](/docs/guides/tagger) con este conjunto de datos.
- **Editar datos** abre el editor de conjuntos de datos.
- **Compartir** abre el cuadro de diálogo para compartir.
- **Cambiar nombre** cambia el nombre del conjunto de datos.

Otras acciones son **Guardar una copia** y **Eliminar**.

> [!WARNING]
> Eliminar quita el conjunto de datos de tu biblioteca de forma permanente. No puedes deshacerlo. Las ejecuciones creadas a partir del conjunto de datos siguen siendo válidas, pero su enlace a él se rompe.

## Editar un conjunto de datos

El editor muestra el conjunto de datos como una cuadrícula. Muestra 100 filas en cada página. Usa **Filas siguientes** y **Filas anteriores** para pasar de una página a otra.

### Cambiar filas y columnas

- **Añadir fila** y **Añadir columna** añaden celdas nuevas al final.
- **Insertar fila arriba** e **Insertar fila abajo** añaden una fila junto a la fila seleccionada.
- **Duplicar fila**, **Eliminar fila**, **Eliminar columna** y **Cambiar nombre de columna** cambian la estructura.
- **Arrastra para rellenar** copia el valor de una celda en las celdas contiguas.
- **Deshacer** y **Rehacer** revierten o repiten tu último cambio.

Haz doble clic en una fila para verla completa.

### Guardado

El editor guarda tus cambios automáticamente, poco después de cada edición. El estado muestra **Guardando…** mientras guarda. Si el guardado falla, el estado muestra **Error al guardar**.

> [!NOTE]
> El editor no puede guardar un conjunto de datos sin filas. Muestra «Agrega una fila para guardar los cambios» hasta que añadas una.

Haz clic en **Volver a la biblioteca** cuando termines.

### Acceso de solo lectura

Si tienes el rol de Lector en un conjunto de datos compartido, el editor se abre en modo de solo lectura. Para cambiar los datos, pide al propietario el rol de Editor, o usa **Guardar una copia**.

### Etiquetar con IA

El editor tiene la acción **Etiquetar con IA**. Abre el etiquetador, donde un modelo de IA te ayuda a añadir etiquetas. Consulta [Etiquetar datos con el etiquetador](/docs/guides/tagger).

## Fijar los roles de columna

Una ejecución debe saber qué columnas son entradas y qué columna es la salida esperada. Fijas estos roles cuando envías una ejecución, no en el editor de conjuntos de datos.

En el asistente de envío, el paso «Cómo leer los datos» muestra cada columna con tres opciones:

- **Entrada**: tu evaluador recibe este valor en `case["input"]`.
- **Salida**: la respuesta esperada con la que compara el evaluador.
- **Omitir**: la ejecución no tiene en cuenta esta columna.

Los valores predeterminados son:

- Todas las columnas son entradas, y la última columna es la salida.
- Un conjunto de datos con una sola columna tiene una entrada y ninguna salida.

Sin columna de salida, Skynet usa la primera fila como una única entrada. Debes elegir al menos una columna de entrada.

Algunas columnas también pueden contener imágenes. Para esas columnas, elige **Imagen** o **Texto** para indicar a Skynet cómo leer los valores.

> [!TIP]
> El editor de conjuntos de datos muestra una insignia «salida» en las columnas de salida. Una columna nueva que añades en el editor empieza como entrada.

El asistente también divide las filas en partes para el entrenamiento y la evaluación. Las partes deben sumar 1. Si no suman 1, el asistente muestra «La división debe sumar 1.» Para el flujo completo, lee [Iniciar una ejecución con el asistente](/docs/guides/submit).

## Compartir un conjunto de datos

Selecciona un conjunto de datos y haz clic en **Compartir**. Se abre el cuadro de diálogo **Compartir y privacidad**.

### Acceso del enlace

| Opción                                | Quién puede abrir el conjunto de datos                                     |
| ------------------------------------- | -------------------------------------------------------------------------- |
| **Restringido**                       | Solo el propietario y las personas invitadas.                              |
| **Cualquier persona con el enlace**   | Cualquier persona con sesión iniciada que tenga el enlace, con el rol que elijas. |

Para un conjunto de datos con «Cualquier persona con el enlace», elige el rol que da el enlace: **Lector** o **Editor**. Haz clic en **Copiar enlace** para copiar la URL.

### Invitar a personas

1. En **Invitar a personas**, escribe un nombre de usuario en **Añade personas por nombre de usuario**.
2. Haz clic en **Invitar**.
3. Revisa la lista en **Personas con acceso**. Usa **Cambiar rol** para cambiar el rol de una persona.

Los roles son:

| Rol         | Acceso                                                                  |
| ----------- | ----------------------------------------------------------------------- |
| Propietario | Control total, incluida la gestión del uso compartido y la eliminación. |
| Editor      | El acceso de Lector, más ejecutar el modelo, chatear y cambiar el nombre. |
| Lector      | Abre el conjunto de datos. El editor es de solo lectura.                |

### Transferir la propiedad

Usa **Transferir la propiedad** para dar el conjunto de datos a otra persona. Skynet te pide que lo confirmes.

> [!WARNING]
> Después de la transferencia, pasas a ser editor y pierdes los privilegios de propietario.

## Próximos pasos

- [Inicia una ejecución con el asistente](/docs/guides/submit) con tu conjunto de datos.
- [Etiqueta datos con el etiquetador](/docs/guides/tagger) si tus filas aún no tienen etiquetas.
- Lee sobre los [evaluadores](/docs/concepts/scorers) para elegir cómo compara Skynet las salidas.
