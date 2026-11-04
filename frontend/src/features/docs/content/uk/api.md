HTTP API Skynet дає змогу скриптам та інструментам робити те саме, що й вебзастосунок: запускати оптимізації, стежити за ними й читати результати. Ця сторінка показує, як підписувати запити, де знайти повний довідник, і містить один повний приклад.

> [!TIP]
> Ви використовуєте AI-агента, наприклад Claude Code, Codex або Cursor? Тоді підключіть його через [MCP](/docs/mcp). Агент сам запускатиме оптимізації й стежитиме за ними, з лімітом витрат для кожного запуску.

## Базова URL-адреса

Надсилайте кожен запит на:

```text
https://api.skynetml.com
```

## Автентифікація

API використовує API-токени.

1. Відкрийте **Налаштування** у Skynet, а потім вкладку **API**.
2. Натисніть **Згенерувати токен**.
3. Одразу скопіюйте токен. Skynet показує повний токен лише один раз.

Передавайте токен у заголовку `Authorization` кожного запиту:

```http
Authorization: Bearer skyd_...
```

- Токени починаються з `skyd_`.
- Кожен користувач має щонайбільше один дійсний токен. Новий токен анулює старий.
- Щоб анулювати токен, натисніть **Відкликати токен**.

> [!WARNING]
> Поводьтеся з токеном як з паролем. Не зберігайте його у вихідному коді й журналах. Як Skynet зберігає токени, описано в розділі [Безпека](/docs/security).

## Інтерактивний довідник

Повний довідник доступний за адресою [api.skynetml.com/scalar](https://api.skynetml.com/scalar). У ньому перелічено кожну публічну кінцеву точку, її поля й відповіді. З цієї сторінки можна надсилати тестові запити.

Довідник читає документ OpenAPI за адресою `https://api.skynetml.com/openapi.public.json`. Використовуйте цей файл, щоб згенерувати клієнт для своєї мови програмування.

## Основні кінцеві точки

| Метод і шлях                                    | Що робить                                                   |
| ----------------------------------------------- | ----------------------------------------------------------- |
| `GET /models`                                   | Повертає список доступних вам моделей.                      |
| `GET /optimizers`                               | Повертає список оптимізаторів, які підходять для налаштувань. |
| `GET /blackbox/engines`                         | Повертає список нативних рушіїв і доступність кожного з них. |
| `POST /optimize` | Запускає оптимізацію для програми DSPy. |
| `POST /blackbox/run`                            | Запускає оптимізацію на репозиторії GitHub.                 |
| `GET /optimizations`                            | Повертає список ваших запусків.                             |
| `GET /optimizations/{optimization_id}`          | Повертає один запуск: статус, прогрес, журнали й результат. |
| `GET /optimizations/{optimization_id}/artifact` | Повертає найкращу версію, яку знайшов запуск.               |
| `POST /optimizations/{optimization_id}/cancel`  | Зупиняє запуск.                                             |
| `POST /serve/{optimization_id}`                 | Виконує найкращу версію на нових вхідних даних.             |
| `GET /health`                                   | Показує, чи працює сервіс.                                  |

## Створення запуску

`POST /optimize` приймає ті самі налаштування, що й майстер надсилання. У прикладі покращується програма DSPy, яка розподіляє повідомлення служби підтримки за мітками.

```bash tab="curl"
curl https://api.skynetml.com/optimize \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "task": {
    "objective": "Sort each support message into billing, bug or other.",
    "artifact": {
      "kind": "dspy_program",
      "starting_version": "Label this support message as billing, bug or other."
    }
  },
  "data": {
    "mode": "pairs",
    "cases": [
      { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
      { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
      {
        "input": { "message": "Do you have a Hebrew interface?" },
        "expected": { "label": "other" }
      }
    ]
  },
  "optimizer": { "choice": "auto", "budget": { "max_scorer_runs": 200 } },
  "models": {
    "task_model": { "name": "<model from GET /models>" },
    "reflection_model": { "name": "<model from GET /models>" }
  },
  "workspace": {
    "run_code": "import dspy\n\nprogram = dspy.Predict(\"message -> label\")\n\n\ndef run(artifact, input):\n    return program(message=input[\"message\"]).label\n",
    "metric_code": "def score(input, output, expected=None):\n    ok = str(output).strip().lower() == expected[\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n"
  },
  "max_cost_cents": 500
}
```

Важливі поля:

- `task.artifact.kind` має значення `dspy_program`.
- `data.mode` має значення `pairs` (вхідні дані з очікуваними результатами) або `single` (рівно один кейс).
- `optimizer.choice` має значення `auto` або назву одного оптимізатора з `GET /optimizers`.
- `max_cost_cents` — ліміт витрат для цього запуску в центах США.
- `is_private` за замовчуванням має значення `false`. Установіть `true`, щоб зробити запуск приватним.

Правила для `run_code` і `metric_code` описано в розділі [Метрики й оцінювачі](/docs/concepts/scorers).

### Відповіді

| Статус | Значення                                                                                                                                              |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet поставив запуск у чергу. Тіло відповіді містить `optimization_id` і `status`.                                                                  |
| `202`  | Перевірка налаштувань ще триває. Зачекайте стільки секунд, скільки вказано в заголовку `Retry-After`. Потім надішліть те саме тіло з тим самим `Idempotency-Key`. |
| `402`  | Ваш баланс замалий.                                                                                                                                   |
| `409`  | Ви досягли квоти, або перевірка налаштувань застаріла.                                                                                                |
| `422`  | Запит або код робочого простору не підходить для оптимізатора.                                                                                        |

> [!NOTE]
> Завжди надсилайте заголовок `Idempotency-Key`. Якщо ви повторно надішлете той самий запит із тим самим ключем, Skynet поверне перший запуск. Другий запуск він не створить.

## Відстеження запуску

Опитуйте запуск, доки його статус не стане підсумковим:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Поле `status` має одне з таких значень:

| Статус                             | Підсумковий |
| ---------------------------------- | ----------- |
| `pending`, `validating`, `running` | Ні          |
| `paused`                           | Ні          |
| `success`                          | Так         |
| `failed`, `cancelled`, `stopped`   | Так         |

Опитуйте кожні 10–30 секунд. Запуск може тривати від кількох хвилин до кількох годин.

У [довіднику API](https://api.skynetml.com/scalar) перелічено всі поля відповіді про запуск.

## Використання результату

Коли статус — `success`, отримайте найкращу версію:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Щоб виконати найкращу версію на нових вхідних даних, надішліть `POST /serve/{optimization_id}`. Що означає кожна оцінка, описано в розділі [Читання результатів](/docs/guides/results).

## Ліміти

API використовує ті самі квоти, обмеження частоти запитів і тарифікацію, що й вебзастосунок. Прочитайте [Ліміти](/docs/limits) і [Бюджети й оплата](/docs/concepts/billing).
