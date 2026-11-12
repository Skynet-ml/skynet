Un conjunto de datos es una tabla de casos. Cada fila es un caso. Skynet puntúa cada versión de tu repositorio sobre los casos con tu evaluador y usa las puntuaciones para comparar versiones. Esta página explica cómo lee Skynet los casos, qué hacen los roles de columna y cómo divide los casos en los conjuntos de entrenamiento, validación y prueba.

## Cargar los datos

En el paso **Entradas** del asistente, tienes dos opciones:

- **Sube un archivo CSV, JSON o Excel**. Skynet acepta archivos `.csv`, `.json`, `.xlsx` y `.xls`.
- **Elegir de la biblioteca**. Usa un conjunto de datos que guardaste antes.

Una ejecución puede tener como máximo 200.000 casos.

Lee [Preparar un conjunto de datos](/docs/guides/datasets) para gestionar los conjuntos de datos guardados.

## Roles de columna

En **Cómo leer los datos**, asigna un rol a cada columna:

| Rol     | Qué hace Skynet con la columna                                   |
| ------- | ---------------------------------------------------------------- |
| Entrada | Pone el valor en `case["input"]`, que recibe tu evaluador      |
| Salida  | Pone el valor en `case["expected"]`, que recibe tu evaluador               |
| Omitir  | No usa la columna                                                |

Cada columna tiene además un tipo: **Texto** o **Imagen**.

Solo una columna puede ser la columna de salida. Si marcas una segunda columna como Salida, la primera pasa a Omitir.

### Qué recibe tu código

Para cada fila, Skynet construye un caso:

- `input` es un dict. Tiene una clave por cada columna de entrada.
- `expected` es un dict con una sola clave: el nombre de la columna de salida.

Para esta tabla:

| message             | label   | id  |
| ------------------- | ------- | --- |
| I was charged twice | billing | 17  |

con los roles `message` = Entrada, `label` = Salida e `id` = Omitir, tu código recibe:

```python tab="input"
{"message": "I was charged twice"}
```

```python tab="expected"
{"label": "billing"}
```

> [!TIP]
> En tu evaluador, lee el valor esperado por el nombre de la columna, por ejemplo `case["expected"]["label"]`.

## Modos de datos

Los roles fijan el modo de datos de la ejecución:

| Modo   | Cuándo                         | Qué ocurre                                                                                               |
| ------ | ------------------------------ | -------------------------------------------------------------------------------------------------------- |
| Pares  | Una columna es de salida       | Cada fila es un caso con una entrada y un valor esperado. Necesitas al menos 2 filas.                    |
| Único  | Ninguna columna es de salida   | La ejecución usa solo las columnas de entrada de la primera fila. Es un único caso sin valor esperado.   |

En el modo único, Skynet usa ese único caso para el entrenamiento, la selección y el informe. No se reserva nada.

## La división

En el modo de pares, Skynet divide los casos en tres conjuntos:

| Conjunto            | Para qué sirve                                                 |
| ------------------- | -------------------------------------------------------------- |
| Entrenamiento       | El motor propone y prueba versiones con estos casos.           |
| Val (validación)    | Skynet selecciona la mejor versión con estos casos.            |
| Prueba              | Skynet informa la puntuación final con estos casos.            |

> [!NOTE]
> Los casos de prueba nunca llegan a un motor, a un proponente ni a la comprobación del contrato. La puntuación final se calcula con casos que el optimizador no vio.

### La división predeterminada

| Número de pares | Entrenamiento | Val | Prueba |
| --------------- | ------------- | --- | ------ |
| 5 o más         | 70 %          | 15 % | 15 %  |
| Menos de 5      | 85 %          | 15 % | 0 %   |

Con menos de 5 pares, Skynet no reserva un conjunto de prueba.

### Cómo cuenta Skynet los casos

Skynet convierte las fracciones en números enteros de casos con el método del mayor resto. Los recuentos siempre suman el número de casos. Un conjunto con una fracción mayor que cero recibe al menos un caso. Un conjunto con una fracción de cero no recibe ningún caso.

Antes de la división, Skynet baraja los casos. El barajado usa la semilla de la ejecución. Si no das una semilla, Skynet genera una a partir de los casos, el objetivo y el artefacto. Así, si vuelves a enviar la misma ejecución, obtienes la misma división.

### Alternativas

- Con menos de 5 pares, Skynet elimina el conjunto de prueba y escala entrenamiento y validación para que sumen 1.
- Si el conjunto de entrenamiento está vacío, Skynet usa los casos de validación como entrenamiento.
- Si no hay conjunto de validación, Skynet selecciona con los casos de entrenamiento.
- Si no hay conjunto de prueba, Skynet informa con validación. Si tampoco hay conjunto de validación, informa con entrenamiento.

> [!WARNING]
> Una puntuación que Skynet informa con entrenamiento o validación no es una puntuación sobre datos reservados. Usa al menos 5 pares para tener un conjunto de prueba.

## Cambiar la división

La sección de la división muestra **Partición recomendada**. Tiene dos modos:

1. **Usar recomendación**: Skynet usa la división predeterminada descrita arriba.
2. **Selección manual**: tú fijas **Entrenamiento**, **Validación** y **Prueba**.

En la selección manual, cada campo acepta una fracción de 0 a 1, en pasos de 0,05. Cada campo muestra el número de casos que recibe. Las tres fracciones deben sumar 1. Si no suman 1, el asistente muestra «La división debe sumar 1.»

## Muestras del contrato

La comprobación del contrato del paso **Métrica y ejecución** ejecuta tu configuración y tu evaluador sobre la versión inicial, en 3 casos de muestra. Skynet toma estas muestras solo de los casos de entrenamiento y validación, nunca de prueba.

## Páginas relacionadas

- [Preparar un conjunto de datos](/docs/guides/datasets)
- [Métricas y evaluadores](/docs/concepts/scorers)
- [Iniciar una ejecución con el asistente](/docs/guides/submit)
- [Límites](/docs/limits)
