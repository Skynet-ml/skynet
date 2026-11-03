معیار به Skynet می‌گوید یک خروجی چقدر خوب است. معیار یک تابع Python در `metric.py` است. همهٔ موتورها همین تابع را فراخوانی می‌کنند. این صفحه قرارداد، مقدار بازگشتی و چند مثال را ارائه می‌دهد.

## قرارداد

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| آرگومان    | محتوای آن                                                              |
| ---------- | ---------------------------------------------------------------------- |
| `input`    | یک dict با فیلدهای ورودی نمونه                                         |
| `output`   | آنچه `run(artifact, input)` شما برای این نمونه برگرداند                 |
| `expected` | مقدار مورد انتظار نمونه، یا `None` وقتی نمونه مقدار مورد انتظار ندارد   |

اگر تابع شما فقط دو آرگومان بگیرد، Skynet آن را به شکل `score(input, output)` فراخوانی می‌کند.

برای یک برنامهٔ DSPy، Skynet فراخوانی معیار DSPy را با همین تابع تطبیق می‌دهد. شما برای هر نوع آرتیفکت فقط یک معیار می‌نویسید. صفحهٔ [بهینه‌سازی چگونه کار می‌کند](/docs/concepts/optimization) را بخوانید.

## مقدار بازگشتی

`score` باید یک جفت برگرداند (یک tuple یا یک list با دو عضو):

1. **امتیاز.** یک عدد. بهینه‌ساز تلاش می‌کند آن را بالاتر ببرد.
2. **اطلاعات جانبی.** یک dict. باید کلید `"feedback"` با مقدار رشته‌ای داشته باشد.

اطلاعات جانبی می‌تواند کلید `"scores"` هم داشته باشد. این کلید نام‌ها را به عددها نگاشت می‌کند، برای مثال `{"format": 1.0, "accuracy": 0.5}`.

اگر مقدار بازگشتی شکل دیگری داشته باشد، بررسی قرارداد در **مقدار بازگشتی معیار** ناموفق می‌شود.

| مقدار بازگشتی                                          | معتبر                                    |
| ------------------------------------------------------ | ---------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | بله                                      |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | بله                                      |
| `0.8`                                                  | خیر. اطلاعات جانبی وجود ندارد.           |
| `(0.8, "Close.")`                                      | خیر. اطلاعات جانبی dict نیست.            |
| `(0.8, {})`                                            | خیر. رشتهٔ feedback وجود ندارد.          |
| `("0.8", {"feedback": "OK"})`                          | خیر. امتیاز یک رشته است.                 |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | خیر. یک امتیاز نام‌دار عدد نیست.         |

## نوشتن بازخورد مفید

GEPA، DSPy GEPA و Auto متن بازخورد را می‌خوانند تا نسخهٔ بعدی را بنویسند. بگویید چه چیزی اشتباه است و خروجی درست چه شکلی دارد.

| بازخورد ضعیف     | بازخورد مفید                                                                         |
| ---------------- | ------------------------------------------------------------------------------------ |
| «اشتباه است.»    | «انتظار 'billing' بود، 'other' دریافت شد. پیام به یک برداشت از حساب اشاره می‌کند.»    |
| «قالب بد است.»   | «خروجی JSON معتبر نیست. بعد از آخرین فیلد یک ویرگول اضافه دارد.»                     |

> [!NOTE]
> بهینه‌ساز هرگز کد منبع معیار شما را نمی‌بیند. با یک بهینه‌ساز DSPy، مدل بازاندیشی فقط متن بازخورد را می‌بیند. مقادیر مورد انتظار شما نزد معیار می‌مانند.

## مثال‌ها

### تطابق دقیق

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` یک dict است که کلید آن نام ستون خروجی شما است. صفحهٔ [مجموعه‌داده‌ها و تقسیم](/docs/concepts/datasets) را بخوانید.

### چند بررسی

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

### مدل در نقش داور

معیار می‌تواند یک مدل را فراخوانی کند. `llm` را از ماژول کمکی `skynet` وارد کنید. فراخوانی‌ای که از معیار انجام می‌شود به مدل معیار می‌رود.

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

معیار مدل خود را در فراخوانی `llm()` خودش تعیین می‌کند. هر چهار آرگومان نمایش‌داده‌شده اختیاری هستند. بدون `model=`، معیار از مدل بازاندیشی استفاده می‌کند.

## دقت، سرعت و هزینه

یک معیار می‌تواند دقت را با زمان اجرای اندازه‌گیری‌شده و هزینهٔ مدل ترکیب کند. `REWARD_SPEC` را در سطح ماژول تعریف کنید و مقدار `reward` را برگردانید.

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

- `weights` باید `accuracy`، `speed` و `cost` را داشته باشد. هر وزن 0 یا بیشتر است و همهٔ وزن‌ها نمی‌توانند 0 باشند. Skynet آن‌ها را طوری مقیاس می‌دهد که مجموعشان 1 شود.
- واحد `speed.reference` میلی‌ثانیه است. واحد `cost.reference` دلار آمریکا است. هر دو باید مثبت باشند.
- بدون `accuracy.reference`، دقت امتیازی از 0 تا 1 است. با آن، دقت یک خطا است و مقدار کمتر بهتر است.

Skynet زمان و هزینه را خودش اندازه می‌گیرد. کد اجرا نمی‌تواند آن‌ها را تغییر دهد. اگر Skynet نتواند زمان یا هزینه را اندازه بگیرد، آن بخش امتیاز 0 می‌گیرد و بازخورد این را اعلام می‌کند.

معیار همچنین می‌تواند اندازه‌گیری‌ها را مستقیماً با `observation()` و `service_cost()` از `skynet` بخواند.

## محدودیت‌ها

| مورد                           | مقدار                                       |
| ------------------------------ | ------------------------------------------- |
| مهلت زمانی معیار               | به‌طور پیش‌فرض 60 ثانیه، حداکثر 600 ثانیه   |
| نمونه‌های بررسی قرارداد         | 3 نمونه                                     |

## صفحه‌های مرتبط

- [مجموعه‌داده‌ها و تقسیم](/docs/concepts/datasets)
- [موتورها](/docs/concepts/engines)
- [ارسال یک اجرا](/docs/guides/submit)
