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
| `POST /blackbox/run`                            | Начинает запуск на репозитории GitHub.                        |
| `GET /optimizations`                            | Возвращает список ваших запусков.                             |
| `GET /optimizations/{optimization_id}`          | Возвращает один запуск: статус, прогресс, логи и результат.   |
| `GET /optimizations/{optimization_id}/artifact` | Возвращает лучшую версию, найденную запуском.                 |
| `POST /optimizations/{optimization_id}/cancel`  | Останавливает запуск.                                         |
| `POST /serve/{optimization_id}`                 | Выполняет лучшую версию на новом входе.                       |
| `GET /health`                                   | Показывает, работает ли сервис.                               |

## Начать запуск

Skynet — это Optimize Anything: каждый запуск оптимизирует репозиторий. `POST /blackbox/run` принимает те же настройки, что и мастер отправки. В примере улучшается репозиторий, который распределяет обращения в поддержку по меткам.

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

Важные поля:

- `target.repo.repository` — репозиторий GitHub в виде `owner/name`. Сначала подключите GitHub в разделе **Настройки → Коннекторы**.
- `target.repo.editable_paths` перечисляет файлы и папки, которые оптимизатор может менять. `"."` означает весь репозиторий.
- `target.repo.commit` необязателен. Это SHA из 40 символов. Без него Skynet использует последний коммит ветки.
- `target.repo.secrets` необязателен. У каждого секрета есть `name` и `value`. Секрет попадает в ваш код и ваш оценщик как переменная окружения.
- `scorer.metric_code` определяет `score(repo_path, case)`. `scorer.model` — модель, которую ваш код и ваш оценщик могут вызывать через шлюз моделей.
- `strategy.mode` — это `auto` (по умолчанию) или `single`. При `single` задайте в `strategy.engine` имя одного движка из `GET /blackbox/engines`.
- `max_cost_cents` — лимит расходов для этого запуска в центах США.
- `is_private` по умолчанию равно `false`. Установите `true`, чтобы запуск был приватным.

Правила для `metric_code` описаны в разделе [Метрики и оценщики](/docs/concepts/scorers).

### Ответы

| Статус | Значение                                                                                                                                                  |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet поставил запуск в очередь. Тело содержит `optimization_id` и `status`.                                                                             |
| `400`  | Цель — не репозиторий, или оценщик не загружается.                                                                                                        |
| `202`  | Проверка настроек ещё идёт. Подождите столько секунд, сколько указано в заголовке `Retry-After`. Затем отправьте то же тело с тем же `Idempotency-Key`. |
| `402`  | Недостаточно средств на балансе.                                                                                                                          |
| `409`  | Вы достигли квоты, или проверка настроек устарела.                                                                                                        |
| `422`  | Запрос не подходит для оптимизатора.                                                                                                                      |

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

Чтобы выполнить лучшую версию на новом входе, отправьте `POST /serve/{optimization_id}` с заголовком `Idempotency-Key`. В репозитории должен быть файл `serve.py` с `def serve(input)`. Запрос, ответ и значение каждой оценки описаны в разделе [Чтение результатов](/docs/guides/results).

## Лимиты

API использует те же квоты, ограничения частоты запросов и правила оплаты, что и веб-приложение. Прочитайте [Лимиты](/docs/limits) и [Бюджеты и оплата](/docs/concepts/billing).
