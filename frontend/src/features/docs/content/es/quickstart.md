Esta página muestra cómo iniciar tu primera ejecución en unos diez minutos. Inicias sesión, subes un conjunto de datos pequeño, completas los cuatro pasos del asistente e inicias la ejecución. El ejemplo mejora un programa DSPy que clasifica mensajes de soporte en etiquetas.

## Antes de empezar

Necesitas:

- Una cuenta de Skynet.
- Un archivo CSV, JSON o Excel (`.csv`, `.json`, `.xlsx` o `.xls`). Cada fila es un caso.
- Una o más columnas de entrada, por ejemplo `message`.
- Una columna de salida esperada, por ejemplo `label`.

> [!TIP]
> Usa al menos 5 filas. Con menos de 5 pares, Skynet no reserva un conjunto de prueba, así que la puntuación final sale de casos que el optimizador ya usó.

## 1. Iniciar sesión

Inicia sesión en Skynet. Después abre la página de envío en `/submit`.

> [!NOTE]
> Esta página usa las etiquetas del nivel **Estándar**. El nivel **Guiado** usa etiquetas más sencillas, omite el paso Optimizador y selecciona **Auto** por ti. Cambia el nivel en Ajustes.

## 2. Tarea

El primer paso es **Tarea**. Pregunta «¿Qué quieres optimizar?».

1. En **Objetivo**, escribe qué es un buen resultado. Usa palabras sencillas, por ejemplo «Clasifica cada mensaje de soporte como facturación, error u otro».
2. Opcional: en **Contexto**, añade información que ayude al optimizador.
3. En **Qué se optimiza**, selecciona **Programa DSPy**.
4. En **Versión inicial**, pega la instrucción actual de tu programa.

## 3. Entradas

El segundo paso es **Entradas**.

1. Selecciona **Sube un archivo CSV, JSON o Excel**, o **Elegir de la biblioteca** para usar un conjunto de datos guardado.
2. En **Cómo leer los datos**, asigna un rol a cada columna:
   - **Entrada**: el código de ejecución recibe esta columna.
   - **Salida**: el valor esperado. La métrica lo recibe.
   - **Omitir**: Skynet no usa esta columna.
3. Mantén **Usar recomendación** para la división. Para la mayoría de los conjuntos de datos, la división es 70 % entrenamiento, 15 % validación y 15 % prueba.

Lee [Conjuntos de datos y divisiones](/docs/concepts/datasets) para conocer todas las reglas.

## 4. Optimizador

El tercer paso es **Optimizador** («Optimizador, modelos y presupuesto»).

- Deja el optimizador en **Auto**.
- Selecciona un **Modelo de reflexión**. El optimizador lo usa para escribir versiones nuevas. Este modelo es obligatorio.
- Selecciona un **Modelo de tarea**. Tu programa lo usa. Un programa DSPy debe tener uno.
- En **Presupuesto**, deja **Máximo de ejecuciones del evaluador** en su valor predeterminado de 200.

Lee [Motores](/docs/concepts/engines) para elegir un motor concreto.

## 5. Métrica y ejecución

El último paso es **Métrica y ejecución** («Código y métrica»). Escribes dos archivos.

```python tab="run.py"
import dspy

program = dspy.Predict("message -> label")


def run(artifact, input):
    return program(message=input["message"]).label
```

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"].strip().lower()
    got = str(output).strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`artifact` es la instrucción actual que propone el optimizador. Skynet la aplica a `program`. `input` contiene las columnas de entrada de un caso. `expected` contiene la columna de salida, con su nombre de columna como clave.

Después selecciona **Comprobar** en **Comprobación del contrato**. La comprobación ejecuta tu código y tu métrica en el sandbox sobre 3 casos de muestra. Comprueba:

| Comprobación                     | Qué falla                                                              |
| -------------------------------- | ---------------------------------------------------------------------- |
| Firma del código de ejecución    | `run.py` no define `run(artifact, input)`                              |
| Firma de la métrica              | `metric.py` no define `score(input, output, expected=None)`            |
| Modelo de la métrica             | El modelo de la métrica no se puede usar                               |
| Código de ejecución en muestras  | Tu código de ejecución lanza un error en un caso de muestra            |
| Valor que devuelve la métrica    | `score` no devuelve una puntuación y unos comentarios válidos          |

> [!NOTE]
> Solo puedes iniciar la ejecución cuando la comprobación del contrato pasa.

## 6. Iniciar y seguir

La pantalla de resumen muestra «Listo para ejecutar». Sus pestañas son General, Datos, Modelos, Presupuesto y Recompensa. Revisa la configuración y luego inicia la optimización.

Abre la ejecución desde tu lista de optimizaciones para seguirla. Lee [Resultados](/docs/guides/results) para entender las puntuaciones y la versión ganadora.

## Próximos pasos

- Escribe una métrica mejor: [Métricas y evaluadores](/docs/concepts/scorers).
- Descubre qué motores encajan con tu artefacto: [Cómo funciona la optimización](/docs/concepts/optimization).
- Descubre cuánto cuesta una ejecución: [Facturación](/docs/concepts/billing).
