Esta página muestra cómo iniciar tu primera ejecución en unos diez minutos. Inicias sesión, subes un conjunto de datos pequeño, completas los cuatro pasos del asistente e inicias la ejecución. En el ejemplo, Skynet crea un repositorio nuevo que clasifica mensajes de soporte en etiquetas y después lo mejora.

## Antes de empezar

Necesitas:

- Una cuenta de Skynet.
- Una cuenta de GitHub. Skynet crea el repositorio en esta cuenta.
- Un archivo CSV, JSON o Excel (`.csv`, `.json`, `.xlsx` o `.xls`). Cada fila es un caso.
- Una o más columnas de entrada, por ejemplo `message`.
- Una columna de salida esperada, por ejemplo `label`.

> [!TIP]
> Usa al menos 5 filas. Con menos de 5 pares, Skynet no reserva un conjunto de prueba, así que la puntuación final sale de casos que el optimizador ya usó.

## 1. Iniciar sesión

Inicia sesión en Skynet. Conecta GitHub en **Ajustes → Conectores**. Después abre la página de envío en `/submit`.

> [!NOTE]
> Esta página usa las etiquetas del nivel **Estándar**. El nivel **Guiado** usa etiquetas más sencillas, omite el paso Optimizador y selecciona **Auto** por ti. Cambia el nivel en Ajustes.

## 2. Tarea

El primer paso es **Tarea**. Pregunta «¿Qué quieres optimizar?».

1. En **Objetivo**, escribe qué es un buen resultado. Usa palabras sencillas, por ejemplo «Clasifica cada mensaje de soporte como facturación, error u otro».
2. Opcional: en **Contexto**, añade información que ayude al optimizador.
3. Selecciona **Crear un repositorio nuevo**. Escribe un nombre, por ejemplo `support-labels`, y selecciona **Privado** o **Público**.

Skynet crea el repositorio en tu cuenta de GitHub. El asistente escribe una primera solución a partir de tu tarea y tus datos. Esta solución es la versión inicial. Si ya tienes código, selecciona **Usar un repositorio**. Lee [Iniciar una ejecución con el asistente](/docs/guides/submit).

## 3. Entradas

El segundo paso es **Entradas**.

1. Selecciona **Sube un archivo CSV, JSON o Excel**, o **Elegir de la biblioteca** para usar un conjunto de datos guardado.
2. En **Cómo leer los datos**, asigna un rol a cada columna:
   - **Entrada**: tu código recibe esta columna.
   - **Salida**: el valor esperado. El evaluador lo recibe.
   - **Omitir**: Skynet no usa esta columna.
3. Mantén **Usar recomendación** para la división. Para la mayoría de los conjuntos de datos, la división es 70 % entrenamiento, 15 % validación y 15 % prueba.

Lee [Conjuntos de datos y divisiones](/docs/concepts/datasets) para conocer todas las reglas.

## 4. Optimizador

El tercer paso es **Optimizador** («Optimizador, modelos y presupuesto»).

- Deja el optimizador en **Auto**.
- Selecciona un **Modelo de reflexión**. El optimizador lo usa para escribir versiones nuevas. Este modelo es obligatorio.
- En **Presupuesto**, deja **Máximo de ejecuciones del evaluador** en su valor predeterminado de 200.

Lee [Motores](/docs/concepts/engines) para elegir un motor concreto.

## 5. Métrica y ejecución

El último paso es **Métrica y ejecución**. El asistente escribe el evaluador por ti. Puedes editarlo. El evaluador recibe la ruta de una versión del repositorio y un caso:

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = str(module.serve(case["input"])).strip().lower()
    want = case["expected"]["label"].strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`case["input"]` contiene las columnas de entrada de un caso. `case["expected"]` contiene la columna de salida, con su nombre de columna como clave. El repositorio tiene un archivo `serve.py` con `def serve(input)`. Skynet usa la misma función para servir la mejor versión después de la ejecución.

Después ejecuta la comprobación. La comprobación ejecuta tu configuración y tu evaluador sobre la versión inicial en el sandbox. Solo puedes iniciar la ejecución cuando la comprobación pasa.

## 6. Iniciar y seguir

La pantalla de resumen muestra «Listo para ejecutar». Sus pestañas son General, Datos, Modelos, Presupuesto y Recompensa. Revisa la configuración y luego inicia la optimización.

Abre la ejecución desde tu lista de optimizaciones para seguirla. Skynet sube cada candidato a su propia rama en tu repositorio nuevo. La rama `skynet/best` siempre apunta a la mejor versión. Lee [Resultados](/docs/guides/results) para entender las puntuaciones y la versión ganadora.

## Próximos pasos

- Escribe un evaluador mejor: [Métricas y evaluadores](/docs/concepts/scorers).
- Descubre cómo funcionan los motores: [Cómo funciona la optimización](/docs/concepts/optimization).
- Descubre cuánto cuesta una ejecución: [Facturación](/docs/concepts/billing).
