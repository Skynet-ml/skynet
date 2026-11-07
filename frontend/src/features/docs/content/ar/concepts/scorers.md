يخبر المُقيِّم Skynet بمدى جودة نسخة واحدة من مستودعك. وهو دالة Python في `scorer.py`. كل المحرّكات تستدعي الدالة نفسها. تشرح هذه الصفحة العقد وقيمة الإرجاع وبعض الأمثلة.

## العقد

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| الوسيطة     | ما تحتويه                                                                       |
| ----------- | ------------------------------------------------------------------------------- |
| `repo_path` | مجلد نسخة واحدة من المستودع، بعد تشغيل أمر الإعداد                               |
| `case`      | dict. يحتوي `case["input"]` على حقول الإدخال. ويحتوي `case["expected"]` على القيمة المتوقعة، أو `None` |

إذا كانت دالتك تأخذ وسيطة واحدة فقط، يستدعي Skynet `score(repo_path)`. استخدم هذا الشكل عندما يشغّل المُقيِّم اختباراته الخاصة ولا يحتاج إلى حالات.

يعمل المُقيِّم في البيئة المعزولة، في مجلد النسخة. يمكنه استيراد كود من المستودع، أو تشغيل أمر، أو استدعاء `serve()` من `serve.py`. اقرأ [كيف يعمل التحسين](/docs/concepts/optimization).

## قيمة الإرجاع

يجب أن تُرجع `score` درجة مع ملاحظات. هذه الأشكال صالحة:

1. زوج: رقم، ونص ملاحظات أو dict فيه المفتاح `"feedback"`.
2. dict فيه رقم تحت `"score"` ونص تحت `"feedback"`.

يجب أن تكون الملاحظات نصًا غير فارغ.

يمكن أن تحتوي المعلومات الجانبية أيضًا على المفتاح `"scores"`. وهو يربط أسماءً بدرجات مسمّاة. لكل درجة مسمّاة درجتها وملاحظاتها الخاصة، مثل `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

يمكن أن تحتوي المعلومات الجانبية أيضًا على المفتاح `"cases"` بالشكل نفسه. كل عنصر فيه هو نتيجة حالة واحدة. يستخدم كل محرّك كل حالة كهدف مستقل.

إذا كان لقيمة الإرجاع شكل مختلف، يفشل الفحص ويعرض السبب.

| قيمة الإرجاع                                                                    | صالحة                                    |
| ------------------------------------------------------------------------------- | ---------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | نعم                                      |
| `(0.8, "Close. Missing the date.")`                                             | نعم                                      |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | نعم                           |
| `0.8`                                                                           | لا. لا توجد ملاحظات.                     |
| `(0.8, {})`                                                                     | لا. لا يوجد نص ملاحظات.                  |
| `("0.8", {"feedback": "OK"})`                                                   | لا. الدرجة نص.                           |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | لا. إحدى الدرجات المسمّاة ليست رقمًا.   |

## اكتب ملاحظات مفيدة

يقرأ GEPA وAuto نص الملاحظات لكتابة النسخة التالية. قل ما هو الخطأ، وكيف يبدو المخرج الصحيح.

| ملاحظات ضعيفة  | ملاحظات مفيدة                                                                  |
| -------------- | ------------------------------------------------------------------------------ |
| "خطأ."         | "المتوقع 'billing'، والناتج 'other'. الرسالة تذكر عملية خصم."                  |
| "تنسيق سيئ."   | "المخرج ليس JSON صالحًا. فيه فاصلة زائدة بعد الحقل الأخير."                    |

> [!NOTE]
> لا يرى وكيل البرمجة أبدًا مصدر مُقيِّمك ولا القيم المتوقعة ولا أسرار التشغيل. يرى الدرجات ونص الملاحظات فقط.

## أمثلة

### تطابق تام

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

`case["expected"]` هو dict مفتاحه اسم عمود المخرج لديك. اقرأ [مجموعات البيانات والتقسيمات](/docs/concepts/datasets).

### تشغيل اختبارات المستودع

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### النموذج كحَكَم

يمكن للمُقيِّم استدعاء نموذج. استورد `llm` من الوحدة المساعدة `skynet`. يمرّ الاستدعاء عبر بوابة النماذج في Skynet إلى نموذج المُقيِّم في التشغيل.

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

يمكن للمُقيِّم أيضًا استخدام أي عميل متوافق مع OpenAI مع متغيرات البيئة `OPENAI_BASE_URL` و`APP_API_KEY` و`APP_MODEL`. تقبل البوابة نموذج المُقيِّم في التشغيل فقط.

## استدعاءات النماذج والأسرار

- تُقاس الاستدعاءات بمفاتيح Skynet على ميزانية التشغيل. بمفتاحك الخاص (BYOK)، يذهب الاستدعاء إلى مفتاح مزوّدك، دون رسوم نموذج من Skynet. اقرأ [الفوترة](/docs/concepts/billing).
- تصل أسرار التشغيل إلى المُقيِّم كمتغيرات بيئة. اسم المتغير هو اسم السر، مثل `os.environ["SEARCH_API_KEY"]`.
- يُخفي Skynet قيم الأسرار في مخرجات المُقيِّم وفي السجلات.

## الحدود

| العنصر                  | القيمة                                   |
| ----------------------- | ---------------------------------------- |
| مهلة المُقيِّم           | 60 ثانية افتراضيًا، و600 على الأكثر      |

## صفحات ذات صلة

- [مجموعات البيانات والتقسيمات](/docs/concepts/datasets)
- [المحرّكات](/docs/concepts/engines)
- [إرسال تشغيل](/docs/guides/submit)
