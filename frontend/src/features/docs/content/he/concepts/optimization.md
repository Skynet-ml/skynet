Skynet הוא Optimize Anything: כל ריצה מייעלת מאגר קוד. מנוע מציע גרסאות חדשות של המאגר, פונקציית הניקוד (Scorer) שלך נותנת ציון לכל גרסה, ו-Skynet שומר את הטובה ביותר.

## החוזה המשותף

לכל ריצה יש מאגר קוד ופונקציית ניקוד. פונקציית הניקוד היא פונקציית Python שמקבלת את הנתיב של גרסה אחת של המאגר ומקרה אחד.

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

- `repo_path` היא התיקייה של גרסה אחת של המאגר, אחרי שפקודת ההתקנה רצה.
- `case` הוא dict. ‏`case["input"]` מכיל את שדות הקלט של מקרה אחד. ‏`case["expected"]` מכיל את הערך הצפוי, או `None` כשאין למקרה ערך כזה.
- `score` מחזיר זוג: מספר, ו-dict עם מחרוזת `"feedback"`.

בתיקיית השורש של המאגר יש קובץ `serve.py` עם `def serve(input)`. פונקציית הניקוד יכולה לקרוא לו, ו-Skynet משתמש בו כדי להפעיל את הגרסה הטובה ביותר כשירות אחרי הריצה.

> [!NOTE]
> סוכן הקוד שכותב את הגרסאות לא רואה את קוד המקור של פונקציית הניקוד, את הערכים הצפויים או את סודות הריצה.

קרא/י את [פונקציות מדידה ופונקציות ציון](/docs/concepts/scorers) לחוזה המלא של פונקציית הניקוד.

## קריאות למודל מהקוד שלך

קוד המאגר שלך ופונקציית הניקוד שלך יכולים לקרוא למודל דרך שער המודלים של Skynet. ה-sandbox נותן נקודת קצה תואמת OpenAI במשתני סביבה:

| משתנה                | מה הוא מכיל                                                   |
| -------------------- | ------------------------------------------------------------- |
| `OPENAI_BASE_URL`    | הכתובת של השער. ‏`APP_GATEWAY_URL` מכיל את אותו ערך.          |
| `APP_API_KEY`        | טוקן לריצה הזו בלבד. זה לא מפתח של ספק.                       |
| `APP_MODEL`          | שם המודל לשליחה. זה מודל הניקוד של הריצה.                     |
| `ANTHROPIC_BASE_URL` | הכתובת של השער עבור SDK של Anthropic.                         |

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

- קריאות על המפתחות של Skynet נמדדות מול תקציב הריצה.
- עם מפתח משלך (BYOK), הקריאה הולכת למפתח של הספק שלך. ‏Skynet לא גובה עמלת מודל.
- השער דוחה בקשות לכל מודל אחר.
- סודות הריצה מגיעים לקוד שלך ולפונקציית הניקוד שלך כמשתני סביבה. שם המשתנה הוא שם הסוד. ‏Skynet מסתיר את ערכי הסודות בפלט.

אין ל-sandbox גישה אחרת לרשת.

## מנועים

כל מנוע עובד על מאגרי קוד:

- **Auto** הוא ברירת המחדל. הוא מנסה כמה מנועים, ואז ממשיך עם GEPA מהגרסה הטובה ביותר.
- **GEPA** מפתח גרסאות בעזרת רפלקציה ושומר חזית Pareto.
- **Best-of-N** מבקש ממודל הרפלקציה הצעות בלתי תלויות ושומר את הטובה ביותר.
- **AutoResearch**,‏ **Meta-Harness**,‏ **AutoSaddler** ו-**ShinkaEvolve** משתמשים בסוכן קוד כדי להציע גרסאות.

המנועים משנים רק את הקבצים בנתיבים שמותר להם לשנות. קרא/י את [מנועים](/docs/concepts/engines) כדי להכיר כל מנוע ואת ההגדרות שלו.

## Auto

**Auto** היא בחירת ברירת המחדל באשף, לכל ריצה.

1. ‏Auto מחפש עם חמישה מנועים: GEPA,‏ AutoResearch,‏ Meta-Harness,‏ AutoSaddler ו-ShinkaEvolve.
2. הוא בוחר את הגרסה הטובה ביותר שהם מצאו.
3. הוא ממשיך עם GEPA מהגרסה הזו עד שהתקציב נגמר.

‏Auto קורא את טקסט המשוב מפונקציית הניקוד שלך.

## מאיפה מגיעה גרסת ההתחלה

| המצב שלך                       | במה להשתמש                                                                       |
| ------------------------------ | -------------------------------------------------------------------------------- |
| יש לך קוד במאגר GitHub         | **שימוש במאגר קיים**. הקוד בענף או בקומיט שבחרת הוא נקודת ההתחלה.                |
| יש לך רק משימה ונתונים          | **יצירת מאגר חדש**. ‏Skynet כותב פתרון ראשון. זו נקודת ההתחלה.                   |

> [!WARNING]
> GEPA ו-Auto לומדים מטקסט המשוב. אם המשוב שלך הוא תמיד אותה מחרוזת, לאופטימייזרים האלה יש פחות מידע לעבוד איתו.

## עמודים קשורים

- [מנועים](/docs/concepts/engines)
- [דאטאסטים וחלוקה](/docs/concepts/datasets)
- [פונקציות מדידה ופונקציות ציון](/docs/concepts/scorers)
- [הפעלת ריצה](/docs/guides/submit)
