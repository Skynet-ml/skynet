ה-HTTP API של Skynet מאפשר לסקריפטים ולכלים לעשות את מה שאפליקציית הווב עושה: להפעיל ריצות, לעקוב אחריהן ולקרוא את התוצאות. העמוד הזה מראה איך לחתום על בקשות, איפה נמצא התיעוד המלא, ודוגמה מלאה אחת.

> [!TIP]
> משתמש/ת בסוכן AI כמו Claude Code,‏ Codex או Cursor? חבר/י אותו דרך [MCP](/docs/mcp) במקום זאת. הסוכן יפעיל ויעקוב אחרי ריצות בשבילך, עם תקרת הוצאה לכל ריצה.

## כתובת הבסיס

שלח/י כל בקשה אל:

```text
https://api.skynetml.com
```

## אימות

ה-API משתמש ב-API tokens.

1. פתח/י את **הגדרות** ב-Skynet, ואז פתח/י את הלשונית **API**.
2. לחץ/י על **צור/צרי טוקן**.
3. העתק/י את ה-token מיד. ‏Skynet מציג את ה-token המלא פעם אחת בלבד.

שלח/י את ה-token בכותרת `Authorization` של כל בקשה:

```http
Authorization: Bearer skyd_...
```

- tokens מתחילים ב-`skyd_`.
- לכל משתמש יש לכל היותר token תקף אחד. token חדש מבטל את ה-token הישן.
- לחץ/י על **בטל/י טוקן** כדי לבטל token.

> [!WARNING]
> יש להתייחס ל-token כמו לסיסמה. אין לשים אותו בקוד המקור ובלוגים. קרא/י את [אבטחה](/docs/security) כדי להבין איך Skynet שומר tokens.

## תיעוד אינטראקטיבי

התיעוד המלא נמצא ב-[api.skynetml.com/scalar](https://api.skynetml.com/scalar). הוא מפרט כל endpoint ציבורי, את השדות שלו ואת התגובות שלו. אפשר לשלוח בקשות בדיקה מהדף הזה.

התיעוד קורא את מסמך ה-OpenAPI ב-`https://api.skynetml.com/openapi.public.json`. אפשר להשתמש בקובץ הזה כדי ליצור לקוח בשפה שלך.

## endpoints עיקריים

| שיטה ונתיב                                      | מה הוא עושה                                       |
| ----------------------------------------------- | ------------------------------------------------- |
| `GET /models`                                   | מציג את המודלים שזמינים לך.                       |
| `GET /optimizers`                               | מציג את האופטימייזרים שמתאימים להגדרות.           |
| `GET /blackbox/engines`                         | מציג את המנועים המובנים ואם כל אחד מהם זמין.      |
| `POST /optimize` | מפעיל ריצה עבור תוכנית DSPy. |
| `POST /blackbox/run`                            | מפעיל ריצה על מאגר GitHub.                        |
| `GET /optimizations`                            | מציג את הריצות שלך.                               |
| `GET /optimizations/{optimization_id}`          | מחזיר ריצה אחת: סטטוס, התקדמות, לוגים ותוצאה.     |
| `GET /optimizations/{optimization_id}/artifact` | מחזיר את הגרסה הטובה ביותר שהריצה מצאה.           |
| `POST /optimizations/{optimization_id}/cancel`  | עוצר ריצה.                                        |
| `POST /serve/{optimization_id}`                 | מריץ את הגרסה הטובה ביותר על קלט חדש.             |
| `GET /health`                                   | מראה אם השירות פעיל.                              |

## הפעלת ריצה

`POST /optimize` מקבל את אותן הגדרות כמו אשף ההגשה. הדוגמה משפרת תוכנית DSPy שממיינת הודעות תמיכה לתוויות.

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

שדות חשובים:

- `task.artifact.kind` הוא `dspy_program`.
- `data.mode` הוא `pairs` (קלטים עם פלטים צפויים) או `single` (מקרה אחד בדיוק).
- `optimizer.choice` הוא `auto` או שם של אופטימייזר אחד מ-`GET /optimizers`.
- `max_cost_cents` היא תקרת ההוצאה של הריצה הזו, בסנטים אמריקאיים.
- `is_private` הוא `false` כברירת מחדל. הגדר/י אותו ל-`true` כדי לשמור על הריצה פרטית.

קרא/י את [פונקציות מדידה ופונקציות ציון](/docs/concepts/scorers) לכללים של `run_code` ו-`metric_code`.

### תגובות

| סטטוס | משמעות                                                                                                                      |
| ----- | --------------------------------------------------------------------------------------------------------------------------- |
| `201` | ‏Skynet הכניס את הריצה לתור. הגוף מכיל `optimization_id` ו-`status`.                                                        |
| `202` | בדיקת ההגדרות עדיין רצה. יש לחכות את מספר השניות שבכותרת `Retry-After`. אחר כך לשלוח את אותו גוף עם אותו `Idempotency-Key`. |
| `402` | היתרה שלך נמוכה מדי.                                                                                                        |
| `409` | הגעת למכסה, או שבדיקת ההגדרות לא עדכנית.                                                                                    |
| `422` | הבקשה או קוד ה-workspace לא מתאימים לאופטימייזר.                                                                            |

> [!NOTE]
> יש לשלוח תמיד כותרת `Idempotency-Key`. כששולחים שוב את אותה בקשה עם אותו מפתח, Skynet יחזיר את הריצה הראשונה. הוא לא יפעיל ריצה שנייה.

## מעקב אחרי ריצה

בדוק/בדקי את הריצה שוב ושוב עד שהסטטוס שלה סופי:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

השדה `status` הוא אחד מאלה:

| סטטוס                                | סופי |
| ------------------------------------ | ---- |
| `pending`,‏ `validating`,‏ `running` | לא   |
| `paused`                             | לא   |
| `success`                            | כן   |
| `failed`,‏ `cancelled`,‏ `stopped`   | כן   |

מומלץ לבדוק כל 10 עד 30 שניות. ריצה יכולה להימשך מדקות עד שעות.

[הפניית ה-API](https://api.skynetml.com/scalar) מציגה כל שדה בתגובת הריצה.

## שימוש בתוצאה

כשהסטטוס הוא `success`, קבל/י את הגרסה הטובה ביותר:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

כדי להריץ את הגרסה הטובה ביותר על קלט חדש, שלח/י `POST /serve/{optimization_id}`. קרא/י את [תוצאות](/docs/guides/results) כדי להבין מה כל ציון אומר.

## מגבלות

ה-API משתמש באותן מכסות, מגבלות קצב וחיוב כמו אפליקציית הווב. קרא/י את [מגבלות](/docs/limits) ואת [חיוב](/docs/concepts/billing).
