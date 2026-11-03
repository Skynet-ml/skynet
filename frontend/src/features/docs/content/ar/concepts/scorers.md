يخبر المقياس Skynet بمدى جودة المخرج. إنه دالة Python في `metric.py`. كل محرّك يستدعي الدالة نفسها. تعرض هذه الصفحة العقد وقيمة الإرجاع وأمثلة.

## العقد

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| الوسيطة    | ما تحتويه                                                           |
| ---------- | ------------------------------------------------------------------- |
| `input`    | dict يحتوي على حقول الإدخال للحالة                                  |
| `output`   | ما أرجعته الدالة `run(artifact, input)` الخاصة بك للحالة            |
| `expected` | القيمة المتوقعة للحالة، أو `None` عندما لا تكون للحالة قيمة متوقعة |

إذا كانت دالتك تأخذ وسيطتين فقط، يستدعي Skynet `score(input, output)`.

لبرنامج DSPy، يكيّف Skynet استدعاء مقياس DSPy مع هذه الدالة نفسها. تكتب مقياسًا واحدًا لكل أنواع العناصر. اقرأ [كيف يعمل التحسين](/docs/concepts/optimization).

## قيمة الإرجاع

يجب أن تُرجع `score` زوجًا (tuple أو list من عنصرين):

1. **الدرجة.** رقم. يحاول المُحسِّن رفعه.
2. **المعلومات الجانبية.** dict. يجب أن يحتوي على المفتاح `"feedback"` بقيمة نصية.

يمكن أن تحتوي المعلومات الجانبية أيضًا على المفتاح `"scores"`. يربط هذا المفتاح أسماءً بأرقام، مثل `{"format": 1.0, "accuracy": 0.5}`.

إذا كان لقيمة الإرجاع شكل مختلف، يفشل فحص العقد عند **قيمة إرجاع المقياس**.

| قيمة الإرجاع                                           | صالحة                                    |
| ------------------------------------------------------ | ---------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | نعم                                      |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | نعم                                      |
| `0.8`                                                  | لا. لا توجد معلومات جانبية.              |
| `(0.8, "Close.")`                                      | لا. المعلومات الجانبية ليست dict.        |
| `(0.8, {})`                                            | لا. لا يوجد نص ملاحظات.                  |
| `("0.8", {"feedback": "OK"})`                          | لا. الدرجة نص.                           |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | لا. إحدى الدرجات المسماة ليست رقمًا.     |

## اكتب ملاحظات مفيدة

يقرأ GEPA وDSPy GEPA وAuto نص الملاحظات ليكتبوا الإصدار التالي. اذكر ما الخطأ وكيف يبدو المخرج الصحيح.

| ملاحظات ضعيفة  | ملاحظات مفيدة                                                                  |
| -------------- | ------------------------------------------------------------------------------ |
| "خطأ."         | "المتوقع 'billing'، والناتج 'other'. الرسالة تذكر عملية خصم."                  |
| "تنسيق سيئ."   | "المخرج ليس JSON صالحًا. فيه فاصلة زائدة بعد الحقل الأخير."                    |

> [!NOTE]
> لا يرى المُحسِّن أبدًا الكود المصدري لمقياسك. مع مُحسِّن DSPy، يرى نموذج التأمل نص الملاحظات فقط. تبقى قيمك المتوقعة مع المقياس.

## أمثلة

### تطابق تام

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` هو dict مفاتيحه اسم عمود الإخراج الخاص بك. اقرأ [مجموعات البيانات والتقسيمات](/docs/concepts/datasets).

### عدة فحوص

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

### النموذج كحَكَم

يمكن للمقياس أن يستدعي نموذجًا. استورد `llm` من الوحدة المساعدة `skynet`. يذهب الاستدعاء من المقياس إلى نموذج المقياس.

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

يحدد المقياس نموذجه في استدعاء `llm()` الخاص به. المعاملات الأربعة المعروضة اختيارية. من دون `model=`، يستخدم المقياس نموذج التأمل.

## الدقة والسرعة والتكلفة

يمكن للمقياس أن يمزج الدقة مع زمن التشغيل المقيس وتكلفة النموذج. عرّف `REWARD_SPEC` على مستوى الوحدة وأرجِع قيمة `reward`.

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

- يجب أن يحتوي `weights` على `accuracy` و`speed` و`cost`. كل وزن يساوي 0 أو أكثر، ولا يمكن أن تكون كلها 0. يعيد Skynet تحجيمها ليكون مجموعها 1.
- `speed.reference` بالمللي ثانية. `cost.reference` بالدولار الأمريكي. يجب أن تكون كلتاهما موجبة.
- بدون `accuracy.reference`، تكون الدقة درجة من 0 إلى 1. ومعها، تكون الدقة خطأً، والقيمة الأقل أفضل.

يقيس Skynet الزمن والتكلفة بنفسه. لا يستطيع كود التشغيل تغييرهما. إذا لم يستطع Skynet قياس الزمن أو التكلفة، يحصل ذلك الجزء على 0 وتذكر الملاحظات ذلك.

يمكن للمقياس أيضًا قراءة القياسات مباشرة باستخدام `observation()` و`service_cost()` من `skynet`.

## الحدود

| العنصر                  | القيمة                                   |
| ----------------------- | ---------------------------------------- |
| مهلة المقياس            | 60 ثانية افتراضيًا، و600 على الأكثر      |
| عيّنات فحص العقد        | 3 حالات                                  |

## صفحات ذات صلة

- [مجموعات البيانات والتقسيمات](/docs/concepts/datasets)
- [المحرّكات](/docs/concepts/engines)
- [إرسال تشغيل](/docs/guides/submit)
