امتیازدهنده به Skynet می‌گوید یک نسخه از مخزن شما چقدر خوب است. امتیازدهنده یک تابع Python در `scorer.py` است. همهٔ موتورها همین تابع را فراخوانی می‌کنند. این صفحه قرارداد، مقدار بازگشتی و چند مثال را ارائه می‌دهد.

## قرارداد

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| آرگومان     | محتوای آن                                                                        |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | پوشهٔ یک نسخه از مخزن، پس از اجرای دستور راه‌اندازی                                |
| `case`      | یک dict. `case["input"]` فیلدهای ورودی را نگه می‌دارد. `case["expected"]` مقدار مورد انتظار را نگه می‌دارد، یا `None` است |

اگر تابع شما فقط یک آرگومان بگیرد، Skynet آن را به شکل `score(repo_path)` فراخوانی می‌کند. وقتی امتیازدهنده آزمون‌های خودش را اجرا می‌کند و به مورد نیاز ندارد، از این شکل استفاده کنید.

امتیازدهنده در محیط اجرای ایزوله و در پوشهٔ همان نسخه اجرا می‌شود. می‌تواند کد را از مخزن import کند، یک دستور اجرا کند یا `serve()` را از `serve.py` فراخوانی کند. صفحهٔ [بهینه‌سازی چگونه کار می‌کند](/docs/concepts/optimization) را بخوانید.

## مقدار بازگشتی

`score` باید یک امتیاز به‌همراه بازخورد برگرداند. این شکل‌ها معتبرند:

1. یک جفت: یک عدد، و یک رشتهٔ بازخورد یا یک dict با کلید `"feedback"`.
2. یک dict با یک عدد زیر `"score"` و یک رشته زیر `"feedback"`.

بازخورد باید رشته‌ای باشد که خالی نیست.

اطلاعات جانبی می‌تواند کلید `"scores"` هم داشته باشد. این کلید نام‌ها را به امتیازهای نام‌دار نگاشت می‌کند. هر امتیاز نام‌دار امتیاز و بازخورد خودش را دارد، برای مثال `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

اطلاعات جانبی می‌تواند کلید `"cases"` هم با همین شکل داشته باشد. هر مدخل نتیجهٔ یک مورد است. هر موتور هر مورد را به‌عنوان یک هدف جداگانه به کار می‌برد.

اگر مقدار بازگشتی شکل دیگری داشته باشد، بررسی ناموفق می‌شود و دلیل را نشان می‌دهد.

| مقدار بازگشتی                                                                   | معتبر                                  |
| ------------------------------------------------------------------------------- | -------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | بله                                    |
| `(0.8, "Close. Missing the date.")`                                             | بله                                    |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | بله                         |
| `0.8`                                                                           | خیر. بازخوردی وجود ندارد.              |
| `(0.8, {})`                                                                     | خیر. رشتهٔ بازخورد وجود ندارد.          |
| `("0.8", {"feedback": "OK"})`                                                   | خیر. امتیاز یک رشته است.               |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | خیر. یک امتیاز نام‌دار عدد نیست.      |

## نوشتن بازخورد مفید

GEPA و Auto متن بازخورد را می‌خوانند تا نسخهٔ بعدی را بنویسند. بگویید چه چیزی اشتباه است و خروجی درست چه شکلی دارد.

| بازخورد ضعیف   | بازخورد مفید                                                                        |
| -------------- | ----------------------------------------------------------------------------------- |
| «اشتباه است.»  | «انتظار 'billing' بود، 'other' دریافت شد. پیام به یک برداشت از حساب اشاره می‌کند.»   |
| «قالب بد است.» | «خروجی JSON معتبر نیست. بعد از آخرین فیلد یک ویرگول اضافه دارد.»                    |

> [!NOTE]
> عامل کدنویسی هرگز کد منبع امتیازدهندهٔ شما، مقدارهای مورد انتظار یا رازهای اجرا را نمی‌بیند. فقط امتیازها و متن بازخورد را می‌بیند.

## مثال‌ها

### تطابق دقیق

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` یک dict است که کلیدهای آن نام ستون «خروجی» شما هستند. صفحهٔ [مجموعه‌داده‌ها و تقسیم](/docs/concepts/datasets) را بخوانید.

### اجرای آزمون‌های مخزن

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### مدل به‌عنوان داور

امتیازدهنده می‌تواند یک مدل را فراخوانی کند. `llm` را از ماژول کمکی `skynet` وارد کنید. فراخوانی از طریق دروازهٔ مدل Skynet به مدل امتیازدهندهٔ اجرا می‌رود.

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
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

امتیازدهنده می‌تواند از هر کلاینت سازگار با OpenAI هم با متغیرهای محیطی `OPENAI_BASE_URL`، `APP_API_KEY` و `APP_MODEL` استفاده کند. دروازه فقط مدل امتیازدهندهٔ اجرا را می‌پذیرد.

## فراخوانی‌های مدل و رازها

- فراخوانی‌هایی که از کلیدهای Skynet استفاده می‌کنند از بودجهٔ اجرا اندازه‌گیری و کسر می‌شوند. با کلید خودتان (BYOK)، فراخوانی به کلید ارائه‌دهندهٔ شما می‌رود و Skynet کارمزد مدل دریافت نمی‌کند. صفحهٔ [صورت‌حساب](/docs/concepts/billing) را بخوانید.
- رازهای اجرا به‌صورت متغیرهای محیطی به امتیازدهنده می‌رسند. نام متغیر همان نام راز است، برای مثال `os.environ["SEARCH_API_KEY"]`.
- ‏Skynet مقدار رازها را در خروجی امتیازدهنده و در لاگ‌ها پنهان می‌کند.

## محدودیت‌ها

| مورد                    | مقدار                                       |
| ----------------------- | ------------------------------------------- |
| مهلت زمانی امتیازدهنده  | به‌طور پیش‌فرض 60 ثانیه، حداکثر 600 ثانیه   |

## صفحه‌های مرتبط

- [مجموعه‌داده‌ها و تقسیم](/docs/concepts/datasets)
- [موتورها](/docs/concepts/engines)
- [ارسال یک اجرا](/docs/guides/submit)
