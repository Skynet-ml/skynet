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
| `POST /blackbox/run`                            | Запускає оптимізацію на репозиторії GitHub.                 |
| `GET /optimizations`                            | Повертає список ваших запусків.                             |
| `GET /optimizations/{optimization_id}`          | Повертає один запуск: статус, прогрес, журнали й результат. |
| `GET /optimizations/{optimization_id}/artifact` | Повертає найкращу версію, яку знайшов запуск.               |
| `POST /optimizations/{optimization_id}/cancel`  | Зупиняє запуск.                                             |
| `POST /serve/{optimization_id}`                 | Виконує найкращу версію на нових вхідних даних.             |
| `GET /health`                                   | Показує, чи працює сервіс.                                  |

## Створення запуску

Skynet — це Optimize Anything: кожен запуск оптимізує репозиторій. `POST /blackbox/run` приймає ті самі налаштування, що й майстер надсилання. У прикладі покращується репозиторій, який розподіляє повідомлення служби підтримки за мітками.

```bash tab="curl"
curl https://api.skynetml.com/blackbox/run \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "objective": "Sort each support message into billing, bug or other.",
  "recipe": "repo",
  "target": {
    "kind": "repo",
    "repo": {
      "provider": "github",
      "repository": "your-org/support-labels",
      "branch": "main",
      "editable_paths": ["serve.py", "prompts/"]
    }
  },
  "cases": [
    { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
    { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
    {
      "input": { "message": "Do you have a Hebrew interface?" },
      "expected": { "label": "other" }
    }
  ],
  "input_fields": ["message"],
  "expected_field": "label",
  "scorer": {
    "kind": "python",
    "metric_code": "import importlib.util\n\n\ndef score(repo_path, case):\n    spec = importlib.util.spec_from_file_location(\"serve\", f\"{repo_path}/serve.py\")\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    ok = str(module.serve(case[\"input\"])).strip().lower() == case[\"expected\"][\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n",
    "model": { "name": "<model from GET /models>" }
  },
  "strategy": { "mode": "auto" },
  "budget": { "max_scorer_runs": 200 },
  "reflection_model_config": { "name": "<model from GET /models>" },
  "max_cost_cents": 500
}
```

Важливі поля:

- `target.repo.repository` — це репозиторій GitHub у форматі `owner/name`. Спочатку підключіть GitHub у **Налаштування → Конектори**.
- `target.repo.editable_paths` перелічує файли й папки, які оптимізатор може змінювати. `"."` означає весь репозиторій.
- `target.repo.commit` необов’язкове. Це SHA із 40 символів. Без нього Skynet використовує останній коміт у гілці.
- `target.repo.secrets` необов’язкове. Кожен секрет має `name` і `value`. Він потрапляє у ваш код і ваш оцінювач як змінна середовища.
- `scorer.metric_code` визначає `score(repo_path, case)`. `scorer.model` — це модель, яку ваш код і ваш оцінювач можуть викликати через шлюз моделей.
- `strategy.mode` має значення `auto` (за замовчуванням) або `single`. Для `single` укажіть у `strategy.engine` назву одного рушія з `GET /blackbox/engines`.
- `max_cost_cents` — ліміт витрат для цього запуску в центах США.
- `is_private` за замовчуванням має значення `false`. Установіть `true`, щоб зробити запуск приватним.

Правила для `metric_code` описано в розділі [Метрики й оцінювачі](/docs/concepts/scorers).

### Відповіді

| Статус | Значення                                                                                                                                              |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet поставив запуск у чергу. Тіло відповіді містить `optimization_id` і `status`.                                                                  |
| `400`  | Ціль не є репозиторієм, або оцінювач не завантажується. |
| `202`  | Перевірка налаштувань ще триває. Зачекайте стільки секунд, скільки вказано в заголовку `Retry-After`. Потім надішліть те саме тіло з тим самим `Idempotency-Key`. |
| `402`  | Ваш баланс замалий.                                                                                                                                   |
| `409`  | Ви досягли квоти, або перевірка налаштувань застаріла.                                                                                                |
| `422`  | Запит не підходить для оптимізатора.                                                                                        |

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

Щоб виконати найкращу версію на нових вхідних даних, надішліть `POST /serve/{optimization_id}` із заголовком `Idempotency-Key`. Репозиторій має містити файл `serve.py` з `def serve(input)`. Запит, відповідь і значення кожної оцінки описано в розділі [Читання результатів](/docs/guides/results).

## Ліміти

API використовує ті самі квоти, обмеження частоти запитів і тарифікацію, що й вебзастосунок. Прочитайте [Ліміти](/docs/limits) і [Бюджети й оплата](/docs/concepts/billing).
