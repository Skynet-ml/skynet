HTTP API Skynet позволяет скриптам и инструментам делать то же, что и веб-приложение: начинать запуски, следить за ними и читать результаты. На этой странице описано, как подписывать запросы, где найти полный справочник, и приведён один полный пример.

> [!TIP]
> Вы используете ИИ-агента, например Claude Code, Codex или Cursor? Подключите его через [MCP](/docs/mcp). Тогда агент сам начинает запуски и следит за ними, с лимитом расходов для каждого запуска.

## Базовый URL

Отправляйте все запросы на:

```text
https://api.skynetml.com
```

## Аутентификация

API использует API-токены.

1. Откройте **Настройки** в Skynet, затем вкладку **API**.
2. Нажмите **Сгенерировать токен**.
3. Сразу скопируйте токен. Skynet показывает полный токен только один раз.

Передавайте токен в заголовке `Authorization` каждого запроса:

```http
Authorization: Bearer skyd_...
```

- Токены начинаются с `skyd_`.
- У каждого пользователя не больше одного действующего токена. Новый токен отключает старый.
- Чтобы отключить токен, нажмите **Отозвать токен**.

> [!WARNING]
> Обращайтесь с токеном как с паролем. Не храните его в исходном коде и логах. Как Skynet хранит токены, описано в разделе [Безопасность](/docs/security).

## Интерактивный справочник

Полный справочник находится по адресу [api.skynetml.com/scalar](https://api.skynetml.com/scalar). В нём перечислены все публичные эндпоинты, их поля и ответы. С этой страницы можно отправлять тестовые запросы.

Справочник читает документ OpenAPI по адресу `https://api.skynetml.com/openapi.public.json`. Используйте этот файл, чтобы сгенерировать клиент на своём языке.

## Основные эндпоинты

| Метод и путь                                    | Что делает                                                    |
| ----------------------------------------------- | ------------------------------------------------------------- |
| `GET /models`                                   | Возвращает список доступных вам моделей.                      |
| `GET /optimizers`                               | Возвращает оптимизаторы, подходящие для настройки.            |
| `GET /blackbox/engines`                         | Возвращает нативные движки и доступность каждого из них.      |
| `POST /optimize` | Запускает оптимизацию для программы DSPy. |
| `POST /blackbox/run`                            | Начинает запуск на репозитории GitHub.                        |
| `GET /optimizations`                            | Возвращает список ваших запусков.                             |
| `GET /optimizations/{optimization_id}`          | Возвращает один запуск: статус, прогресс, логи и результат.   |
| `GET /optimizations/{optimization_id}/artifact` | Возвращает лучшую версию, найденную запуском.                 |
| `POST /optimizations/{optimization_id}/cancel`  | Останавливает запуск.                                         |
| `POST /serve/{optimization_id}`                 | Выполняет лучшую версию на новом входе.                       |
| `GET /health`                                   | Показывает, работает ли сервис.                               |

## Начать запуск

`POST /optimize` принимает те же настройки, что и мастер отправки. В примере улучшается программа DSPy, которая распределяет обращения в поддержку по меткам.

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

Важные поля:

- `task.artifact.kind` — это `dspy_program`.
- `data.mode` — это `pairs` (входы с ожидаемыми выходами) или `single` (ровно один пример).
- `optimizer.choice` — это `auto` или имя одного оптимизатора из `GET /optimizers`.
- `max_cost_cents` — лимит расходов для этого запуска в центах США.
- `is_private` по умолчанию равно `false`. Установите `true`, чтобы запуск был приватным.

Правила для `run_code` и `metric_code` описаны в разделе [Метрики и оценщики](/docs/concepts/scorers).

### Ответы

| Статус | Значение                                                                                                                                                  |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet поставил запуск в очередь. Тело содержит `optimization_id` и `status`.                                                                             |
| `202`  | Проверка настроек ещё идёт. Подождите столько секунд, сколько указано в заголовке `Retry-After`. Затем отправьте то же тело с тем же `Idempotency-Key`. |
| `402`  | Недостаточно средств на балансе.                                                                                                                          |
| `409`  | Вы достигли квоты, или проверка настроек устарела.                                                                                                        |
| `422`  | Запрос или код рабочей области не подходит для оптимизатора.                                                                                              |

> [!NOTE]
> Всегда передавайте заголовок `Idempotency-Key`. Если вы повторно отправите тот же запрос с тем же ключом, Skynet вернёт первый запуск и не начнёт второй.

## Наблюдение за запуском

Опрашивайте запуск, пока его статус не станет окончательным:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Поле `status` принимает одно из значений:

| Статус                             | Окончательный |
| ---------------------------------- | ------------- |
| `pending`, `validating`, `running` | Нет           |
| `paused`                           | Нет           |
| `success`                          | Да            |
| `failed`, `cancelled`, `stopped`   | Да            |

Опрашивайте каждые 10–30 секунд. Запуск может длиться от нескольких минут до нескольких часов.

В [справочнике API](https://api.skynetml.com/scalar) перечислены все поля ответа о запуске.

## Использование результата

Когда статус равен `success`, получите лучшую версию:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Чтобы выполнить лучшую версию на новом входе, отправьте `POST /serve/{optimization_id}`. Что означает каждая оценка, описано в разделе [Чтение результатов](/docs/guides/results).

## Лимиты

API использует те же квоты, ограничения частоты запросов и правила оплаты, что и веб-приложение. Прочитайте [Лимиты](/docs/limits) и [Бюджеты и оплата](/docs/concepts/billing).
