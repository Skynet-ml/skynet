يخبر المُقيِّم Skynet بمدى جودة نسخة واحدة من مستودعك. وهو دالة Python في `scorer.py`. كل المحرّكات تستدعي الدالة نفسها. تشرح هذه الصفحة العقد وقيمة الإرجاع وبعض الأمثلة.

## العقد

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| الوسيطة     | ما تحتويه                                                                       |
| ----------- | ------------------------------------------------------------------------------- |
| `repo_path` | مجلد يحتوي ملفات المقياس فقط. لا يحتوي أي ملف آخر من المستودع ولا كود أي نسخة |
| `case`      | dict. يحتوي `case["input"]` على حقول الإدخال. ويحتوي `case["expected"]` على القيمة المتوقعة، أو `None` |
| `output`    | ما أرجعه `entry` في `[run]` للحالة، أو `None` إذا لم يحتوِ الملف على `[run]` |

يجب أن يحتوي ملف `ascent.toml` في المستودع على قسم `[run]` أو مرحلة `[target.<name>]`. يشغّل `entry` في `[run]` نسخة واحدة على حالة واحدة، في صندوق معزول خاص به. يرفض Skynet الملف الذي لا يحتوي أيًّا منهما، لأن كل نسخة ستحصل على الدرجة نفسها.

- مدخل من نوع دالة، مثل `entry = "serve.py:serve"`، يتلقى `case["input"]` كوسيطة. و`output` هو قيمة الإرجاع منه.
- مدخل من نوع أمر، مثل `entry = ["python", "run_tests.py"]`، يقرأ `{"input": ...}` من ملف JSON في المسار الموجود في `ASCENT_INPUT`. ويكتب `{"output": ...}` في المسار الموجود في `ASCENT_OUTPUT`. و`output` هو القيمة تحت `"output"`. وما يطبعه الأمر يذهب إلى السجلات فقط.

يعمل المُقيِّم في صندوق معزول منفصل. لا يحتوي هذا الصندوق إلا ملفات المقياس. لا يستطيع المُقيِّم استيراد كود النسخة ولا تشغيله. يرى النسخة فقط من خلال `output`، أو من خلال `call()` إلى مرحلة `[target.<name>]`. استورد `call` باستخدام `from openascent import call`. اقرأ [كيف يعمل التحسين](/docs/concepts/optimization).

## قيمة الإرجاع

تُرجع `score` كائنًا واحدًا:

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

- المفتاح `"score"` إلزامي. وهو رقم منتهٍ. هذه الدرجة وحدها ترتّب النسخ.
- المفتاح `"feedback"` نص اختياري. يُنصح به، لأن GEPA وAuto يتعلّمان منه. بدون ملاحظات، ترى المحرّكات الدرجات فقط، ويذكر سجل التشغيل ذلك مرة واحدة.
- المفتاح `"scores"` اختياري. وهو يربط أسماءً بدرجات مسمّاة. كل درجة مسمّاة كائن له `"score"` خاص به و`"feedback"` اختياري. يستخدم كل محرّك كل درجة مسمّاة كهدف مستقل.

أي شكل أو مفتاح آخر خطأ. يعرض الفحص الجزء الخاطئ والشكل الصالح.

| قيمة الإرجاع | صالحة |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | نعم |
| `{"score": 0.8}` | نعم. لا توجد ملاحظات. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | نعم |
| `0.8` | لا. القيمة ليست كائنًا. |
| `(0.8, "Close. Missing the date.")` | لا. القيمة ليست كائنًا. |
| `{"score": "0.8"}` | لا. الدرجة نص. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | لا. يجب أن تكون الدرجة المسمّاة كائنًا. |
| `{"score": 0.8, "feedbak": "OK"}` | لا. المفتاح `feedbak` غير معروف. |

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

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` هو dict مفتاحه اسم عمود المخرج لديك. اقرأ [مجموعات البيانات والتقسيمات](/docs/concepts/datasets).

### تشغيل اختبارات المستودع

يشغّل `entry` في `[run]` الاختبارات. ينتهي `pytest` برمز خروج غير صفري عندما يفشل اختبار، ورمز الخروج غير الصفري يُفشل الحالة. لذلك يكتب المُغلِّف `run_tests.py` النتيجة في `ASCENT_OUTPUT` وينتهي بالرمز 0. ويقرأ المُقيِّم تلك النتيجة.

```toml tab="ascent.toml"
version = 1

[run]
entry = ["python", "run_tests.py"]
timeout = 300
```

```python tab="run_tests.py"
import json
import os
import subprocess

result = subprocess.run(["python", "-m", "pytest", "-q"], capture_output=True, text=True)
with open(os.environ["ASCENT_OUTPUT"], "w") as f:
    json.dump({"output": {"passed": result.returncode == 0, "log": result.stdout[-2000:]}}, f)
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["passed"]:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": output["log"]}
```

عندما يفشل `entry` في `[run]` على حالة، لا تحصل تلك الحالة على درجة. إذا لم يبدأ `entry`، أو تجاوز المهلة، أو أوقفته إشارة، أو إذا رفع `entry` من نوع الدالة استثناءً أو خرج قبل أن يُرجع قيمة، تكون الملاحظات `Run failed: …`، ولا يستدعي Skynet المُقيِّم. ويفشل `entry` من نوع الأمر أيضًا عندما ينتهي برمز خروج غير صفري أو لا يكتب ملف مخرجات صالحًا. النسخة التي فيها حالة كهذه لا تحصل على درجة. لا يمكن أن تكون أفضل نسخة، ولا تصل إلى أي `[outcome] target`، ولا تفتح أي pull request. إذا كان يجب أن يُحسب الفشل رقمًا، فالتقط الفشل في برنامجك، واكتب مخرجًا، ودع المُقيِّم يعطيه درجة. اقرأ [قواعد المستودع](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md).

### النموذج كحَكَم

يمكن للمُقيِّم استدعاء نموذج. استورد `llm` من الوحدة المساعدة `openascent`. يمرّ الاستدعاء عبر بوابة النماذج في Skynet إلى نموذج المُقيِّم في التشغيل.

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from openascent import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

يمكن للمُقيِّم أيضًا استخدام أي عميل متوافق مع OpenAI مع متغيرات البيئة `OPENAI_BASE_URL` و`APP_API_KEY` و`APP_MODEL`. تقبل البوابة نموذج المُقيِّم في التشغيل فقط.

## استدعاءات النماذج والأسرار

- تُقاس الاستدعاءات بمفاتيح Skynet على ميزانية التشغيل. بمفتاحك الخاص (BYOK)، يذهب الاستدعاء إلى مفتاح مزوّدك، دون رسوم نموذج من Skynet. اقرأ [الفوترة](/docs/concepts/billing).
- لا يتلقى المُقيِّم أسرار التشغيل كمتغيرات بيئة. يصل السر إلى المُقيِّم فقط عبر جدول `[services.<name>]` يذكره `[metric] services`، أو عبر خادم في ملف `[metric] mcp`. اكتب السر في `headers` الجدول بالشكل `${NAME}`. يرسل المُقيِّم طلباته إلى عنوان URL الموجود في `ASCENT_SERVICE_<NAME>_URL`. يضيف Skynet السر خارج البيئة المعزولة.
- يُخفي Skynet قيم الأسرار في مخرجات المُقيِّم وفي السجلات.

## الحدود

| العنصر                  | القيمة                                   |
| ----------------------- | ---------------------------------------- |
| مهلة المُقيِّم           | 60 ثانية افتراضيًا، و600 على الأكثر      |

## صفحات ذات صلة

- [مجموعات البيانات والتقسيمات](/docs/concepts/datasets)
- [المحرّكات](/docs/concepts/engines)
- [إرسال تشغيل](/docs/guides/submit)
