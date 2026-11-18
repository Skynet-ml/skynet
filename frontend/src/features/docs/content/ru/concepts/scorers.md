Оценщик сообщает Skynet, насколько хороша одна версия вашего репозитория. Это функция Python в `scorer.py`. Все движки вызывают одну и ту же функцию. На этой странице описаны контракт, возвращаемое значение и примеры.

## Контракт

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Аргумент    | Что содержит                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Папка одной версии репозитория после выполнения команды настройки               |
| `case`      | dict. `case["input"]` содержит входные поля. `case["expected"]` содержит ожидаемое значение или `None` |

Если ваша функция принимает только один аргумент, Skynet вызывает `score(repo_path)`. Используйте эту форму, когда оценщик запускает свои тесты и примеры ему не нужны.

Оценщик работает в песочнице, в папке версии. Он может импортировать код из репозитория, выполнить команду или вызвать `serve()` из `serve.py`. Прочитайте [Как работает оптимизация](/docs/concepts/optimization).

## Возвращаемое значение

`score` возвращает один объект:

```python
{
    "score": 0.78,
    "feedback": "Misses short names.",
    "scores": {
        "precision": {"score": 0.90},
        "recall": {"score": 0.69, "feedback": "Misses names under 4 letters."},
    },
}
```

- `"score"` обязателен. Это конечное число. Только эта оценка упорядочивает версии.
- `"feedback"` — необязательный текст. Его рекомендуется давать, потому что GEPA и Auto учатся на нём. Без обратной связи движки видят только оценки, и журнал запуска сообщает об этом один раз.
- `"scores"` необязателен. Он сопоставляет имена с именованными оценками. Каждая именованная оценка — это объект со своим `"score"` и необязательным `"feedback"`. Каждый движок использует каждую именованную оценку как отдельную цель.

Любая другая форма или ключ — ошибка. Проверка показывает неверную часть и допустимую форму.

| Возвращаемое значение | Допустимо |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | Да |
| `{"score": 0.8}` | Да. Обратной связи нет. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | Да |
| `0.8` | Нет. Значение — не объект. |
| `(0.8, "Close. Missing the date.")` | Нет. Значение — не объект. |
| `{"score": "0.8"}` | Нет. Оценка — это строка. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | Нет. Именованная оценка должна быть объектом. |
| `{"score": 0.8, "feedbak": "OK"}` | Нет. Ключ `feedbak` неизвестен. |

## Полезная обратная связь

GEPA и Auto читают текст обратной связи, чтобы написать следующую версию. Пишите, что не так и как выглядит правильный выход.

| Слабая обратная связь | Полезная обратная связь                                                            |
| --------------------- | ---------------------------------------------------------------------------------- |
| «Неверно».            | «Ожидалось 'billing', получено 'other'. В сообщении упоминается списание».         |
| «Плохой формат».      | «Выход — невалидный JSON. После последнего поля стоит лишняя запятая».             |

> [!NOTE]
> Агент для кода никогда не видит исходный код вашего оценщика, ожидаемые значения и секреты запуска. Он видит только оценки и текст обратной связи.

## Примеры

### Точное совпадение

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` — это dict, ключ которого — имя вашего столбца «Выход». Прочитайте [Наборы данных и разбиение](/docs/concepts/datasets).

### Запуск тестов репозитория

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
```

### Модель как судья

Оценщик может вызывать модель. Импортируйте `llm` из вспомогательного модуля `skynet`. Вызов идёт через шлюз моделей Skynet к модели оценщика этого запуска.

```python tab="scorer.py"
import importlib.util

from skynet import llm


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.serve(case["input"])
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {answer}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

Оценщик также может использовать любой OpenAI-совместимый клиент с переменными окружения `OPENAI_BASE_URL`, `APP_API_KEY` и `APP_MODEL`. Шлюз принимает только модель оценщика этого запуска.

## Вызовы моделей и секреты

- Вызовы на ключах Skynet учитываются в бюджете запуска. С вашим собственным ключом (BYOK) вызов идёт на ключ вашего провайдера, без платы Skynet за модель. Прочитайте [Бюджеты и оплата](/docs/concepts/billing).
- Секреты запуска попадают в оценщик как переменные окружения. Имя переменной совпадает с именем секрета, например `os.environ["SEARCH_API_KEY"]`.
- Skynet скрывает значения секретов в выводе оценщика и в логах.

## Ограничения

| Параметр                        | Значение                                   |
| ------------------------------- | ------------------------------------------ |
| Тайм-аут оценщика               | 60 секунд по умолчанию, не больше 600      |

## Связанные страницы

- [Наборы данных и разбиение](/docs/concepts/datasets)
- [Движки](/docs/concepts/engines)
- [Запуск через мастер](/docs/guides/submit)
