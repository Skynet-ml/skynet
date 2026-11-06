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
| `POST /blackbox/run`                            | מפעיל ריצה על מאגר GitHub.                        |
| `GET /optimizations`                            | מציג את הריצות שלך.                               |
| `GET /optimizations/{optimization_id}`          | מחזיר ריצה אחת: סטטוס, התקדמות, לוגים ותוצאה.     |
| `GET /optimizations/{optimization_id}/artifact` | מחזיר את הגרסה הטובה ביותר שהריצה מצאה.           |
| `POST /optimizations/{optimization_id}/cancel`  | עוצר ריצה.                                        |
| `POST /serve/{optimization_id}`                 | מריץ את הגרסה הטובה ביותר על קלט חדש.             |
| `GET /health`                                   | מראה אם השירות פעיל.                              |

## הפעלת ריצה

‏Skynet הוא Optimize Anything: כל ריצה מייעלת מאגר קוד. ‏`POST /blackbox/run` מקבל את אותן הגדרות כמו אשף ההגשה. הדוגמה משפרת מאגר שממיין הודעות תמיכה לתוויות.

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

שדות חשובים:

- ‏`target.repo.repository` הוא מאגר GitHub בצורה `owner/name`. קודם חבר/י את GitHub ב-**הגדרות → חיבורים**.
- ‏`target.repo.editable_paths` מפרט את הקבצים והתיקיות שהאופטימייזר רשאי לשנות. ‏`"."` פירושו כל המאגר.
- ‏`target.repo.commit` הוא אופציונלי. זה SHA באורך 40 תווים. בלעדיו, Skynet משתמש בקומיט האחרון בענף.
- ‏`target.repo.secrets` הוא אופציונלי. לכל סוד יש `name` ו-`value`. הסוד מגיע לקוד שלך ולפונקציית הניקוד שלך כמשתנה סביבה.
- ‏`scorer.metric_code` מגדיר `score(repo_path, case)`. ‏`scorer.model` הוא המודל שהקוד שלך ופונקציית הניקוד שלך יכולים לקרוא לו דרך שער המודלים.
- ‏`strategy.mode` הוא `auto` (ברירת המחדל) או `single`. עם `single`, הגדר/י את `strategy.engine` לשם של מנוע אחד מ-`GET /blackbox/engines`.
- `max_cost_cents` היא תקרת ההוצאה של הריצה הזו, בסנטים אמריקאיים.
- `is_private` הוא `false` כברירת מחדל. הגדר/י אותו ל-`true` כדי לשמור על הריצה פרטית.

קרא/י את [פונקציות מדידה ופונקציות ציון](/docs/concepts/scorers) לכללים של `metric_code`.

### תגובות

| סטטוס | משמעות                                                                                                                      |
| ----- | --------------------------------------------------------------------------------------------------------------------------- |
| `201` | ‏Skynet הכניס את הריצה לתור. הגוף מכיל `optimization_id` ו-`status`.                                                        |
| `400` | היעד הוא לא מאגר, או שאי אפשר לטעון את פונקציית הניקוד.                                                                     |
| `202` | בדיקת ההגדרות עדיין רצה. יש לחכות את מספר השניות שבכותרת `Retry-After`. אחר כך לשלוח את אותו גוף עם אותו `Idempotency-Key`. |
| `402` | היתרה שלך נמוכה מדי.                                                                                                        |
| `409` | הגעת למכסה, או שבדיקת ההגדרות לא עדכנית.                                                                                    |
| `422` | הבקשה לא מתאימה לאופטימייזר.                                                                                                |

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

כדי להריץ את הגרסה הטובה ביותר על קלט חדש, שלח/י `POST /serve/{optimization_id}` עם כותרת `Idempotency-Key`. במאגר חייב להיות קובץ `serve.py` עם `def serve(input)`. קרא/י את [תוצאות](/docs/guides/results) לגבי הבקשה, התשובה ומה כל ציון אומר.

## מגבלות

ה-API משתמש באותן מכסות, מגבלות קצב וחיוב כמו אפליקציית הווב. קרא/י את [מגבלות](/docs/limits) ואת [חיוב](/docs/concepts/billing).
