تتيح واجهة Skynet HTTP API للسكربتات والأدوات أن تفعل ما يفعله تطبيق الويب: بدء عمليات التشغيل، ومتابعتها، وقراءة النتائج. توضّح هذه الصفحة كيف توقّع الطلبات، وأين تجد المرجع الكامل، وتقدّم مثالًا كاملًا واحدًا.

> [!TIP]
> هل تستخدم وكيل ذكاء اصطناعي مثل Claude Code أو Codex أو Cursor؟ اربطه عبر [MCP](/docs/mcp) بدلًا من ذلك. عندها يبدأ الوكيل عمليات التشغيل ويتابعها نيابةً عنك، مع حد إنفاق لكل تشغيل.

## عنوان URL الأساسي

أرسل كل طلب إلى:

```text
https://api.skynetml.com
```

## المصادقة

تستخدم الواجهة رموز API.

1. افتح **الإعدادات** في Skynet، ثم افتح علامة التبويب **API**.
2. انقر **توليد رمز**.
3. انسخ الرمز فورًا. يعرض Skynet الرمز الكامل مرة واحدة فقط.

أرسل الرمز في الترويسة `Authorization` مع كل طلب:

```http
Authorization: Bearer skyd_...
```

- تبدأ الرموز بـ `skyd_`.
- لكل مستخدم رمز صالح واحد على الأكثر. الرمز الجديد يُبطل الرمز القديم.
- انقر **إبطال الرمز** لإيقاف رمز.

> [!WARNING]
> تعامل مع الرمز مثل كلمة المرور. أبقِه خارج الكود المصدري والسجلات. اقرأ [الأمان](/docs/security) لتعرف كيف يخزّن Skynet الرموز.

## المرجع التفاعلي

المرجع الكامل موجود في [api.skynetml.com/scalar](https://api.skynetml.com/scalar). يسرد كل نقطة نهاية عامة وحقولها واستجاباتها. يمكنك إرسال طلبات تجريبية من تلك الصفحة.

يقرأ المرجع مستند OpenAPI الموجود في `https://api.skynetml.com/openapi.public.json`. استخدم هذا الملف لتوليد عميل بلغتك.

## نقاط النهاية الرئيسية

| الطريقة والمسار                                 | ما الذي تفعله                                             |
| ----------------------------------------------- | --------------------------------------------------------- |
| `GET /models`                                   | تسرد النماذج التي يمكنك استخدامها.                         |
| `GET /optimizers`                               | تسرد المُحسِّنات المناسبة لإعداد معيّن.                     |
| `GET /blackbox/engines`                         | تسرد المحرّكات الأصلية وتوضّح إن كان كل منها متاحًا.        |
| `POST /optimize` | يبدأ تشغيلًا لبرنامج DSPy. |
| `POST /blackbox/run`                            | تبدأ تشغيلًا على مستودع GitHub.                            |
| `GET /optimizations`                            | تسرد عمليات التشغيل الخاصة بك.                             |
| `GET /optimizations/{optimization_id}`          | تُرجع تشغيلًا واحدًا: الحالة والتقدّم والسجلات والنتيجة.     |
| `GET /optimizations/{optimization_id}/artifact` | تُرجع أفضل نسخة وجدها التشغيل.                             |
| `POST /optimizations/{optimization_id}/cancel`  | توقف تشغيلًا.                                              |
| `POST /serve/{optimization_id}`                 | تشغّل أفضل نسخة على إدخال جديد.                            |
| `GET /health`                                   | توضّح إن كانت الخدمة تعمل.                                 |

## بدء تشغيل

تأخذ `POST /optimize` الإعداد نفسه الذي يأخذه معالج الإرسال. يحسّن المثال برنامج DSPy يصنّف رسائل الدعم إلى تسميات.

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

حقول مهمة:

- قيمة `task.artifact.kind` هي `dspy_program`.
- قيمة `data.mode` هي `pairs` (مُدخلات مع مخرجات متوقعة) أو `single` (حالة واحدة بالضبط).
- قيمة `optimizer.choice` هي `auto` أو اسم مُحسِّن واحد من `GET /optimizers`.
- الحقل `max_cost_cents` هو حد الإنفاق لهذا التشغيل، بالسنت الأمريكي.
- قيمة `is_private` الافتراضية هي `false`. اضبطها على `true` لإبقاء التشغيل خاصًا.

اقرأ [المقاييس والمُقيِّمات](/docs/concepts/scorers) لمعرفة قواعد `run_code` و`metric_code`.

### الاستجابات

| الحالة | المعنى                                                                                                                       |
| ------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `201`  | وضع Skynet التشغيل في قائمة الانتظار. يحتوي الجسم على `optimization_id` و`status`.                                           |
| `202`  | فحص الإعداد ما زال قيد التنفيذ. انتظر عدد الثواني المذكور في الترويسة `Retry-After`. ثم أرسل الجسم نفسه مع `Idempotency-Key` نفسه. |
| `402`  | رصيدك منخفض جدًا.                                                                                                            |
| `409`  | بلغت حصة استخدام، أو أن فحص الإعداد قديم.                                                                                    |
| `422`  | الطلب أو كود مساحة العمل لا يناسب المُحسِّن.                                                                                  |

> [!NOTE]
> أرسل دائمًا الترويسة `Idempotency-Key`. إذا أرسلت الطلب نفسه مرة أخرى بالمفتاح نفسه، يُرجع Skynet التشغيل الأول. ولا يبدأ تشغيلًا ثانيًا.

## متابعة تشغيل

استعلم عن التشغيل دوريًا حتى تصبح حالته نهائية:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

قيمة الحقل `status` واحدة مما يلي:

| الحالة                             | نهائية |
| ---------------------------------- | ------ |
| `pending`, `validating`, `running` | لا     |
| `paused`                           | لا     |
| `success`                          | نعم    |
| `failed`, `cancelled`, `stopped`   | نعم    |

استعلم كل 10 إلى 30 ثانية. قد يستغرق التشغيل من دقائق إلى ساعات.

يسرد [مرجع API](https://api.skynetml.com/scalar) كل حقل في استجابة التشغيل.

## استخدام النتيجة

عندما تكون الحالة `success`، احصل على أفضل نسخة:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

لتشغيل أفضل نسخة على إدخال جديد، أرسل `POST /serve/{optimization_id}`. اقرأ [النتائج](/docs/guides/results) لتعرف معنى كل درجة.

## الحدود

تستخدم الواجهة حصص الاستخدام وحدود المعدّل والفوترة نفسها التي يستخدمها تطبيق الويب. اقرأ [الحدود](/docs/limits) و[الفوترة](/docs/concepts/billing).
