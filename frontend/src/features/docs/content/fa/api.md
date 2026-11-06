HTTP API در Skynet به اسکریپت‌ها و ابزارها اجازه می‌دهد همان کارهای اپلیکیشن وب را انجام دهند: اجراها را شروع کنند، آن‌ها را پیگیری کنند و نتیجه‌ها را بخوانند. این صفحه نشان می‌دهد درخواست‌ها را چگونه امضا کنید، مرجع کامل کجاست، و یک مثال کامل می‌آورد.

> [!TIP]
> از یک عامل هوش مصنوعی مثل Claude Code، Codex یا Cursor استفاده می‌کنید؟ به جای این کار، آن را از طریق [MCP](/docs/mcp) متصل کنید. عامل سپس اجراها را برای شما شروع و پیگیری می‌کند، با یک سقف هزینه برای هر اجرا.

## نشانی پایه

همهٔ درخواست‌ها را به این نشانی بفرستید:

```text
https://api.skynetml.com
```

## احراز هویت

API از توکن‌های API استفاده می‌کند.

1. در Skynet، **تنظیمات** را باز کنید، سپس زبانهٔ **API** را باز کنید.
2. روی **تولید توکن** کلیک کنید.
3. توکن را فوراً کپی کنید. Skynet توکن کامل را فقط یک بار نشان می‌دهد.

توکن را در سرآیند `Authorization` هر درخواست بفرستید:

```http
Authorization: Bearer skyd_...
```

- توکن‌ها با `skyd_` شروع می‌شوند.
- هر کاربر حداکثر یک توکن معتبر دارد. یک توکن جدید توکن قبلی را از کار می‌اندازد.
- برای از کار انداختن یک توکن، روی **ابطال توکن** کلیک کنید.

> [!WARNING]
> با توکن مثل یک رمز عبور رفتار کنید. آن را از کد منبع و لاگ‌ها دور نگه دارید. برای اینکه بدانید Skynet توکن‌ها را چگونه ذخیره می‌کند، [امنیت](/docs/security) را بخوانید.

## مرجع تعاملی

مرجع کامل در [api.skynetml.com/scalar](https://api.skynetml.com/scalar) است. این مرجع هر نقطهٔ پایانی (endpoint) عمومی، فیلدها و پاسخ‌های آن را فهرست می‌کند. می‌توانید از همان صفحه درخواست‌های آزمایشی بفرستید.

مرجع سند OpenAPI را از `https://api.skynetml.com/openapi.public.json` می‌خواند. از این فایل برای تولید یک کلاینت به زبان برنامه‌نویسی خود استفاده کنید.

## نقاط پایانی اصلی

| متد و مسیر                                      | چه کاری انجام می‌دهد                                        |
| ----------------------------------------------- | ---------------------------------------------------------- |
| `GET /models`                                   | مدل‌هایی را که می‌توانید استفاده کنید فهرست می‌کند.          |
| `GET /optimizers`                               | بهینه‌سازهایی را که با یک تنظیمات سازگارند فهرست می‌کند.     |
| `GET /blackbox/engines`                         | موتورهای بومی و در دسترس بودن هر کدام را فهرست می‌کند.      |
| `POST /optimize` | یک اجرا برای یک برنامهٔ DSPy شروع می‌کند. |
| `POST /blackbox/run`                            | یک اجرا روی یک مخزن GitHub شروع می‌کند.                     |
| `GET /optimizations`                            | اجراهای شما را فهرست می‌کند.                                |
| `GET /optimizations/{optimization_id}`          | یک اجرا را برمی‌گرداند: وضعیت، پیشرفت، لاگ‌ها و نتیجه.      |
| `GET /optimizations/{optimization_id}/artifact` | بهترین نسخه‌ای را که اجرا پیدا کرده برمی‌گرداند.            |
| `POST /optimizations/{optimization_id}/cancel`  | یک اجرا را متوقف می‌کند.                                    |
| `POST /serve/{optimization_id}`                 | بهترین نسخه را روی یک ورودی جدید اجرا می‌کند.               |
| `GET /health`                                   | نشان می‌دهد سرویس در دسترس است یا نه.                       |

## شروع یک اجرا

`POST /optimize` همان تنظیماتی را می‌گیرد که ویزارد ارسال می‌گیرد. مثال این صفحه یک برنامهٔ DSPy را بهتر می‌کند که پیام‌های پشتیبانی را در برچسب‌ها دسته‌بندی می‌کند.

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

فیلدهای مهم:

- `task.artifact.kind` یکی از `dspy_program` است.
- `data.mode` یا `pairs` است (ورودی‌ها همراه با خروجی‌های مورد انتظار) یا `single` (دقیقاً یک مورد).
- `optimizer.choice` یا `auto` است یا نام یکی از بهینه‌سازهای `GET /optimizers`.
- `max_cost_cents` سقف هزینهٔ این اجرا به سنت آمریکاست.
- `is_private` به‌طور پیش‌فرض `false` است. برای خصوصی نگه داشتن اجرا، آن را `true` کنید.

برای قاعده‌های `run_code` و `metric_code`، [معیارها و امتیازدهنده‌ها](/docs/concepts/scorers) را بخوانید.

### پاسخ‌ها

| وضعیت | معنی                                                                                                                                  |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `201` | Skynet اجرا را در صف قرار داد. بدنهٔ پاسخ شامل `optimization_id` و `status` است.                                                      |
| `202` | بررسی تنظیمات هنوز در حال اجراست. به تعداد ثانیه‌های سرآیند `Retry-After` صبر کنید. سپس همان بدنه را با همان `Idempotency-Key` بفرستید. |
| `402` | موجودی شما کافی نیست.                                                                                                                 |
| `409` | به یک سهمیه رسیده‌اید، یا بررسی تنظیمات قدیمی شده است.                                                                                 |
| `422` | درخواست یا کد فضای کار با بهینه‌ساز سازگار نیست.                                                                                       |

> [!NOTE]
> همیشه سرآیند `Idempotency-Key` را بفرستید. اگر همان درخواست را دوباره با همان کلید بفرستید، Skynet اجرای اول را برمی‌گرداند. اجرای دومی شروع نمی‌کند.

## پیگیری یک اجرا

اجرا را تا وقتی وضعیتش نهایی شود، به‌طور دوره‌ای بپرسید (poll):

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

فیلد `status` یکی از این مقدارهاست:

| وضعیت                              | نهایی |
| ---------------------------------- | ----- |
| `pending`، `validating`، `running` | خیر   |
| `paused`                           | خیر   |
| `success`                          | بله   |
| `failed`، `cancelled`، `stopped`   | بله   |

هر ۱۰ تا ۳۰ ثانیه یک بار بپرسید. یک اجرا ممکن است از چند دقیقه تا چند ساعت طول بکشد.

[مرجع API](https://api.skynetml.com/scalar) همهٔ فیلدهای پاسخ اجرا را فهرست می‌کند.

## استفاده از نتیجه

وقتی وضعیت `success` است، بهترین نسخه را بگیرید:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

برای اجرای بهترین نسخه روی یک ورودی جدید، `POST /serve/{optimization_id}` را بفرستید. برای معنی هر امتیاز، [نتیجه‌ها](/docs/guides/results) را بخوانید.

## محدودیت‌ها

API از همان سهمیه‌ها، محدودیت‌های نرخ درخواست و صورت‌حسابِ اپلیکیشن وب استفاده می‌کند. [محدودیت‌ها](/docs/limits) و [صورت‌حساب](/docs/concepts/billing) را بخوانید.
