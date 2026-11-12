Skynet هو Optimize Anything: كل تشغيل يحسّن مستودعًا. يقترح المحرّك نسخًا جديدة من المستودع، ويقيّم مُقيِّمك كل نسخة، ويحتفظ Skynet بأفضل نسخة.

## العقد المشترك

لكل تشغيل مستودع ومُقيِّم. المُقيِّم دالة Python تتلقى مسار نسخة واحدة من المستودع وحالة واحدة.

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.serve(case["input"])
    ok = output == case["expected"]
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `repo_path` هو مجلد نسخة واحدة من المستودع، بعد تشغيل أمر الإعداد.
- `case` قاموس (dict). يحتوي `case["input"]` على حقول الإدخال لحالة واحدة. ويحتوي `case["expected"]` على القيمة المتوقعة، أو `None` إذا لم تكن للحالة قيمة متوقعة.
- تُرجع `score` زوجًا: رقمًا، وقاموسًا فيه سلسلة نصية `"feedback"`.

في جذر المستودع ملف `serve.py` فيه `def serve(input)`. يمكن للمُقيِّم أن يستدعيه، ويستخدمه Skynet لتقديم أفضل نسخة بعد التشغيل.

> [!NOTE]
> لا يستطيع وكيل البرمجة الذي يكتب النسخ رؤية مصدر المُقيِّم ولا القيم المتوقعة ولا أسرار التشغيل.

اقرأ [المقاييس والمُقيِّمات](/docs/concepts/scorers) للاطلاع على عقد المُقيِّم الكامل.

## استدعاءات النماذج من كودك

يمكن لكود مستودعك ولمُقيِّمك استدعاء نموذج عبر بوابة النماذج في Skynet. تعطي البيئة المعزولة نقطة نهاية متوافقة مع OpenAI في متغيرات البيئة:

| المتغير              | ما يحتويه                                                  |
| -------------------- | ---------------------------------------------------------- |
| `OPENAI_BASE_URL`    | عنوان URL للبوابة. يحتوي `APP_GATEWAY_URL` على القيمة نفسها. |
| `APP_API_KEY`        | رمز (token) لهذا التشغيل فقط. وهو ليس مفتاح مزوّد.          |
| `APP_MODEL`          | اسم النموذج الذي ترسله. وهو نموذج المُقيِّم في التشغيل.      |
| `ANTHROPIC_BASE_URL` | عنوان URL للبوابة لحزم SDK من Anthropic.                    |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["APP_API_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["APP_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- تُقاس الاستدعاءات بمفاتيح Skynet على ميزانية التشغيل.
- بمفتاحك الخاص (BYOK)، يذهب الاستدعاء إلى مفتاح مزوّدك. لا تفرض Skynet رسومًا على النموذج.
- ترفض البوابة الطلبات لأي نموذج آخر.
- تصل أسرار التشغيل إلى كودك وإلى مُقيِّمك كمتغيرات بيئة. اسم المتغير هو اسم السر. يُخفي Skynet قيم الأسرار في المخرجات.

لا يوجد للبيئة المعزولة أي وصول آخر إلى الشبكة.

## المحرّكات

كل محرّك يعمل على المستودعات:

- **Auto** هو الخيار الافتراضي. يجرّب عدة محرّكات، ثم يتابع GEPA من أفضل نسخة.
- يطوّر **GEPA** النسخ بالتأمل ويحتفظ بجبهة باريتو.
- يطلب **Best-of-N** من نموذج التأمل مقترحات مستقلة ويحتفظ بأفضلها.
- تستخدم **AutoResearch** و**Meta-Harness** و**AutoSaddler** و**ShinkaEvolve** وكيل برمجة لاقتراح النسخ.

تغيّر المحرّكات الملفات في المسارات التي تسمح بها فقط. اقرأ [المحرّكات](/docs/concepts/engines) للاطلاع على كل محرّك وإعداداته.

## Auto

**Auto** هو الاختيار الافتراضي في المعالج، لكل تشغيل.

1. يستكشف Auto بخمسة محرّكات: GEPA وAutoResearch وMeta-Harness وAutoSaddler وShinkaEvolve.
2. يختار أفضل نسخة وجدتها.
3. يتابع GEPA من تلك النسخة حتى تنفد الميزانية.

يقرأ Auto نص الملاحظات من مُقيِّمك.

## من أين تأتي نسخة البداية

| حالتك                              | استخدم                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------- |
| لديك كود في مستودع GitHub           | **استخدام مستودع**. الكود في الفرع أو الـ commit الذي تختاره هو نقطة البداية.     |
| لديك مهمة وبيانات فقط               | **إنشاء مستودع جديد**. يكتب Skynet حلًا أوليًا. وهذا الحل هو نقطة البداية.        |

> [!WARNING]
> يتعلّم GEPA وAuto من نص الملاحظات. إذا كانت ملاحظاتك دائمًا السلسلة النصية نفسها، فستكون لدى هذين المُحسِّنين معلومات أقل للعمل بها.

## صفحات ذات صلة

- [المحرّكات](/docs/concepts/engines)
- [مجموعات البيانات والتقسيمات](/docs/concepts/datasets)
- [المقاييس والمُقيِّمات](/docs/concepts/scorers)
- [إرسال تشغيل](/docs/guides/submit)
