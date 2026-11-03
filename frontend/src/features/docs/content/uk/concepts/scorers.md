Метрика повідомляє Skynet, наскільки добрий результат. Це функція Python у файлі `metric.py`. Кожен рушій викликає ту саму функцію. На цій сторінці описано контракт, значення, яке повертає функція, і приклади.

## Контракт

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Аргумент   | Що містить                                                           |
| ---------- | -------------------------------------------------------------------- |
| `input`    | Словник із вхідними полями прикладу                                  |
| `output`   | Те, що ваша функція `run(artifact, input)` повернула для прикладу    |
| `expected` | Очікуване значення прикладу або `None`, якщо приклад його не має     |

Якщо ваша функція приймає лише два аргументи, Skynet викликає `score(input, output)`.

Для програми DSPy Skynet адаптує виклик метрики DSPy до цієї самої функції. Ви пишете одну метрику для будь-якого виду артефакту. Прочитайте [Як працює оптимізація](/docs/concepts/optimization).

## Значення, яке повертає функція

`score` має повертати пару (кортеж або список із двох елементів):

1. **Оцінка.** Число. Оптимізатор намагається його збільшити.
2. **Додаткова інформація.** Словник. Він має містити ключ `"feedback"` із рядковим значенням.

Додаткова інформація також може містити ключ `"scores"`. Він зіставляє назви з числами, наприклад `{"format": 1.0, "accuracy": 0.5}`.

Якщо значення має іншу форму, перевірка контракту не проходить на кроці **Значення, яке повертає метрика**.

| Значення, яке повертає функція                         | Чи коректне                              |
| ------------------------------------------------------ | ---------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Так                                      |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Так                                      |
| `0.8`                                                  | Ні. Немає додаткової інформації.         |
| `(0.8, "Close.")`                                      | Ні. Додаткова інформація — не словник.   |
| `(0.8, {})`                                            | Ні. Немає рядка з відгуком.              |
| `("0.8", {"feedback": "OK"})`                          | Ні. Оцінка — це рядок.                   |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | Ні. Іменована оцінка — не число.         |

## Пишіть корисний відгук

GEPA, DSPy GEPA і Auto читають текст відгуку, щоб написати наступну версію. Вкажіть, що не так і як виглядає правильний результат.

| Слабкий відгук      | Корисний відгук                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------- |
| «Неправильно.»      | «Очікувалося 'billing', отримано 'other'. У повідомленні згадано списання коштів.»           |
| «Поганий формат.»   | «Результат не є коректним JSON. Після останнього поля стоїть зайва кома.»                    |

> [!NOTE]
> Оптимізатор ніколи не бачить вихідного коду вашої метрики. З оптимізатором DSPy модель рефлексії бачить лише текст відгуку. Ваші очікувані значення залишаються в метриці.

## Приклади

### Точний збіг

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` — це словник, ключем у якому є назва вашого стовпця виводу. Прочитайте [Набори даних і розбиття](/docs/concepts/datasets).

### Кілька перевірок

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

### Модель як суддя

Метрика може викликати модель. Імпортуйте `llm` з допоміжного модуля `skynet`. Виклик із метрики надходить до моделі метрики.

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

Метрика задає свою модель у власному виклику `llm()`. Усі чотири показані аргументи необов’язкові. Без `model=` метрика використовує модель рефлексії.

## Точність, швидкість і вартість

Метрика може поєднувати точність із виміряним часом виконання та вартістю моделі. Визначте `REWARD_SPEC` на рівні модуля й поверніть значення `reward`.

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

- `weights` має містити `accuracy`, `speed` і `cost`. Кожна вага дорівнює 0 або більше, і не всі можуть дорівнювати 0. Skynet масштабує їх так, щоб їхня сума дорівнювала 1.
- `speed.reference` задається в мілісекундах. `cost.reference` задається в доларах США. Обидва значення мають бути додатними.
- Без `accuracy.reference` точність — це оцінка від 0 до 1. З ним точність — це похибка, і менше значення краще.

Skynet сам вимірює час і вартість. Код запуску не може їх змінити. Якщо Skynet не може виміряти час або вартість, ця частина отримує 0, і відгук про це повідомляє.

Метрика також може напряму читати виміри через `observation()` і `service_cost()` з `skynet`.

## Обмеження

| Параметр                            | Значення                                   |
| ----------------------------------- | ------------------------------------------ |
| Тайм-аут метрики                    | 60 секунд за замовчуванням, щонайбільше 600 |
| Приклади для перевірки контракту    | 3 приклади                                 |

## Пов’язані сторінки

- [Набори даних і розбиття](/docs/concepts/datasets)
- [Рушії](/docs/concepts/engines)
- [Запуск через майстер](/docs/guides/submit)
