Оцінювач повідомляє Skynet, наскільки добра одна версія вашого репозиторію. Це функція Python у файлі `scorer.py`. Кожен рушій викликає ту саму функцію. На цій сторінці описано контракт, значення, яке повертає функція, і приклади.

## Контракт

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Аргумент    | Що містить                                                                       |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Папка однієї версії репозиторію після виконання команди налаштування              |
| `case`      | Словник. `case["input"]` містить вхідні поля. `case["expected"]` містить очікуване значення або `None` |

Якщо ваша функція приймає лише один аргумент, Skynet викликає `score(repo_path)`. Використовуйте цю форму, коли оцінювач запускає власні тести й не потребує кейсів.

Оцінювач виконується в пісочниці, у папці версії. Він може імпортувати код із репозиторію, запустити команду або викликати `serve()` з `serve.py`. Прочитайте [Як працює оптимізація](/docs/concepts/optimization).

## Значення, яке повертає функція

`score` повертає один об'єкт:

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

- `"score"` обов'язковий. Це скінченне число. Лише ця оцінка впорядковує версії.
- `"feedback"` — необов'язковий текст. Його радимо давати, бо GEPA та Auto вчаться на ньому. Без відгуку рушії бачать лише оцінки, і журнал запуску повідомляє про це один раз.
- `"scores"` необов'язковий. Він зіставляє назви з іменованими оцінками. Кожна іменована оцінка — це об'єкт із власним `"score"` і необов'язковим `"feedback"`. Кожен рушій використовує кожну іменовану оцінку як окрему ціль.

Будь-яка інша форма або ключ — це помилка. Перевірка показує хибну частину й коректну форму.

| Значення, яке повертає функція | Чи коректне |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | Так |
| `{"score": 0.8}` | Так. Відгуку немає. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | Так |
| `0.8` | Ні. Значення — не об'єкт. |
| `(0.8, "Close. Missing the date.")` | Ні. Значення — не об'єкт. |
| `{"score": "0.8"}` | Ні. Оцінка — це рядок. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | Ні. Іменована оцінка має бути об'єктом. |
| `{"score": 0.8, "feedbak": "OK"}` | Ні. Ключ `feedbak` невідомий. |

## Пишіть корисний відгук

GEPA і Auto читають текст відгуку, щоб написати наступну версію. Вкажіть, що не так і як виглядає правильний результат.

| Слабкий відгук      | Корисний відгук                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------- |
| «Неправильно.»      | «Очікувалося 'billing', отримано 'other'. У повідомленні згадано списання коштів.»           |
| «Поганий формат.»   | «Результат не є коректним JSON. Після останнього поля стоїть зайва кома.»                    |

> [!NOTE]
> Агент програмування ніколи не бачить вихідного коду вашого оцінювача, очікуваних значень і секретів запуску. Він бачить лише оцінки й текст відгуку.

## Приклади

### Точний збіг

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

`case["expected"]` — це словник, ключем у якому є назва вашого стовпця виводу. Прочитайте [Набори даних і розбиття](/docs/concepts/datasets).

### Запуск тестів репозиторію

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
```

### Модель як суддя

Оцінювач може викликати модель. Імпортуйте `llm` з допоміжного модуля `skynet`. Виклик іде через шлюз моделей Skynet до моделі оцінювача запуску.

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

Оцінювач також може використовувати будь-який клієнт, сумісний з OpenAI, зі змінними середовища `OPENAI_BASE_URL`, `APP_API_KEY` і `APP_MODEL`. Шлюз приймає лише модель оцінювача запуску.

## Виклики моделей і секрети

- Виклики на ключах Skynet враховуються в бюджеті запуску. З вашим власним ключем (BYOK) виклик іде на ключ вашого провайдера, без плати Skynet за модель. Прочитайте [Бюджети й оплата](/docs/concepts/billing).
- Секрети запуску потрапляють в оцінювач як змінні середовища. Назва змінної — це назва секрету, наприклад `os.environ["SEARCH_API_KEY"]`.
- Skynet приховує значення секретів у виводі оцінювача й у журналах.

## Обмеження

| Параметр                 | Значення                                    |
| ------------------------ | ------------------------------------------- |
| Тайм-аут оцінювача       | 60 секунд за замовчуванням, щонайбільше 600 |

## Пов’язані сторінки

- [Набори даних і розбиття](/docs/concepts/datasets)
- [Рушії](/docs/concepts/engines)
- [Запуск через майстер](/docs/guides/submit)
