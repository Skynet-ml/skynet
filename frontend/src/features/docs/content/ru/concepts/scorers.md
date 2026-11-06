Метрика сообщает Skynet, насколько хорош выход. Это функция Python в файле `metric.py`. Все движки вызывают одну и ту же функцию. На этой странице описаны контракт, возвращаемое значение и примеры.

## Контракт

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Аргумент   | Что содержит                                                       |
| ---------- | ------------------------------------------------------------------ |
| `input`    | Словарь с входными полями примера                                  |
| `output`   | То, что ваш `run(artifact, input)` вернул для этого примера        |
| `expected` | Ожидаемое значение примера или `None`, если его нет                |

Если ваша функция принимает только два аргумента, Skynet вызывает `score(input, output)`.

Для программы DSPy Skynet приводит вызов метрики DSPy к этой же функции. Вы пишете одну метрику для любого вида артефакта. Прочитайте [Как работает оптимизация](/docs/concepts/optimization).

## Возвращаемое значение

`score` должна возвращать пару (кортеж или список из двух элементов):

1. **Оценка.** Число. Оптимизатор старается его увеличить.
2. **Дополнительные сведения.** Словарь. В нём должен быть ключ `"feedback"` со строковым значением.

В дополнительных сведениях может также быть ключ `"scores"`. Он сопоставляет именам числа, например `{"format": 1.0, "accuracy": 0.5}`.

Если возвращаемое значение имеет другую форму, проверка контракта не проходит на пункте **Возвращаемое значение метрики**.

| Возвращаемое значение                                  | Корректно                                       |
| ------------------------------------------------------ | ----------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Да                                              |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Да                                              |
| `0.8`                                                  | Нет. Нет дополнительных сведений.               |
| `(0.8, "Close.")`                                      | Нет. Дополнительные сведения — не словарь.      |
| `(0.8, {})`                                            | Нет. Нет строки обратной связи.                 |
| `("0.8", {"feedback": "OK"})`                          | Нет. Оценка — строка.                           |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | Нет. Именованная оценка — не число.             |

## Полезная обратная связь

GEPA, DSPy GEPA и Auto читают текст обратной связи, чтобы написать следующую версию. Пишите, что не так и как выглядит правильный выход.

| Слабая обратная связь | Полезная обратная связь                                                            |
| --------------------- | ---------------------------------------------------------------------------------- |
| «Неверно».            | «Ожидалось 'billing', получено 'other'. В сообщении упоминается списание».         |
| «Плохой формат».      | «Выход — невалидный JSON. После последнего поля стоит лишняя запятая».             |

> [!NOTE]
> Оптимизатор никогда не видит исходный код вашей метрики. С оптимизатором DSPy модель рефлексии видит только текст обратной связи. Ожидаемые значения остаются у метрики.

## Примеры

### Точное совпадение

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` — словарь, ключ в котором — имя вашего столбца с ролью «Выход». Прочитайте [Наборы данных и разбиение](/docs/concepts/datasets).

### Несколько проверок

```python tab="metric.py"
import json


def score(input, output, expected=None):
    try:
        data = json.loads(output)
    except ValueError as exc:
        return 0.0, {"feedback": f"Output is not valid JSON: {exc}", "scores": {"format": 0.0, "fields": 0.0}}
    want = expected["fields"]
    found = [key for key in want if key in data]
    fields = len(found) / len(want) if want else 1.0
    missing = [key for key in want if key not in data]
    feedback = "All fields present." if not missing else f"Missing fields: {', '.join(missing)}."
    return 0.5 + 0.5 * fields, {"feedback": feedback, "scores": {"format": 1.0, "fields": fields}}
```

### Модель в роли судьи

Метрика может вызывать модель. Импортируйте `llm` из вспомогательного модуля `skynet`. Вызов из метрики идёт к модели метрики.

```python tab="metric.py"
from skynet import llm


def score(input, output, expected=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {input['question']}\nAnswer: {output}",
        model="openai/gpt-4.1-mini",
        temperature=0,
        max_tokens=512,
        reasoning_effort="low",
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

Метрика задаёт свою модель в собственном вызове `llm()`. Все четыре показанных аргумента необязательны. Без `model=` метрика использует модель рефлексии.

## Точность, скорость и стоимость

Метрика может сочетать точность с измеренным временем выполнения и стоимостью модели. Определите `REWARD_SPEC` на уровне модуля и верните значение `reward`.

```python tab="metric.py"
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.7, "speed": 0.2, "cost": 0.1},
    "accuracy": {},
    "speed": {"reference": 2000},
    "cost": {"reference": 0.01},
}


def score(input, output, expected=None):
    ok = output == expected["label"]
    return reward(REWARD_SPEC, 1.0 if ok else 0.0, "Correct." if ok else "Wrong label.")
```

- `weights` должен содержать `accuracy`, `speed` и `cost`. Каждый вес не меньше 0, и не все веса могут быть равны 0. Skynet нормирует их так, чтобы в сумме они давали 1.
- `speed.reference` задаётся в миллисекундах. `cost.reference` задаётся в долларах США. Оба значения должны быть положительными.
- Без `accuracy.reference` точность — это оценка от 0 до 1. С ним точность — это ошибка, и чем меньше, тем лучше.

Skynet сам измеряет время и стоимость. Код запуска не может их изменить. Если Skynet не может измерить время или стоимость, эта часть получает 0, и обратная связь сообщает об этом.

Метрика также может напрямую читать измерения через `observation()` и `service_cost()` из `skynet`.

## Лимиты

| Параметр                        | Значение                                   |
| ------------------------------- | ------------------------------------------ |
| Тайм-аут метрики                | 60 секунд по умолчанию, не больше 600      |
| Примеров в проверке контракта   | 3 примера                                  |

## Связанные страницы

- [Наборы данных и разбиение](/docs/concepts/datasets)
- [Движки](/docs/concepts/engines)
- [Запуск через мастер](/docs/guides/submit)
