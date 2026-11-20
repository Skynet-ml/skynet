פונקציית הניקוד (Scorer) אומרת ל-Skynet כמה טובה גרסה אחת של המאגר שלך. היא פונקציית Python בקובץ `scorer.py`. כל מנוע קורא לאותה פונקציה. העמוד הזה מציג את החוזה, את ערך ההחזרה ודוגמאות.

## החוזה

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| ארגומנט     | מה הוא מכיל                                                                         |
| ----------- | ----------------------------------------------------------------------------------- |
| `repo_path` | תיקייה שמכילה רק את קובצי המדד שלך. אין בה קובץ אחר של המאגר ואין בה קוד של גרסה |
| `case`      | dict.‏ `case["input"]` מכיל את שדות הקלט.‏ `case["expected"]` מכיל את הערך הצפוי, או `None` |
| `output`    | מה שה-`entry` של `[run]` החזיר עבור המקרה, או `None` כשאין בקובץ `[run]` |

בקובץ `ascent.toml` של המאגר חייב להיות קטע `[run]` או שלב `[target.<name>]`. ה-`entry` של `[run]` מריץ גרסה אחת על מקרה אחד, בתיבת sandbox משלו. ‏Skynet דוחה קובץ בלי אף אחד מהם, כי כל גרסה הייתה מקבלת את אותו ציון.

- ‏`entry` של פונקציה, למשל `entry = "serve.py:serve"`, מקבל את `case["input"]` כארגומנט.‏ `output` הוא ערך ההחזרה שלו.
- ‏`entry` של פקודה, למשל `entry = ["python", "run_tests.py"]`, קורא את `{"input": ...}` מקובץ ה-JSON בנתיב שב-`ASCENT_INPUT`.‏ הוא כותב את `{"output": ...}` לנתיב שב-`ASCENT_OUTPUT`.‏ `output` הוא הערך שתחת `"output"`.‏ מה שהפקודה מדפיסה הולך ללוגים בלבד.

פונקציית הניקוד רצה בתיבת sandbox נפרדת. בתיבה הזו יש רק את קובצי המדד שלך. פונקציית הניקוד לא יכולה לייבא או להריץ את הקוד של הגרסה. היא רואה את הגרסה רק דרך `output`, או דרך `call()` לשלב `[target.<name>]`. את `call` מייבאים עם `from openascent import call`. קרא/י את [איך האופטימיזציה עובדת](/docs/concepts/optimization).

## ערך ההחזרה

`score` מחזיר אובייקט אחד:

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

- המפתח `"score"` הוא חובה. הוא מספר סופי. רק הציון הזה קובע את הדירוג של הגרסאות.
- המפתח `"feedback"` הוא טקסט אופציונלי. מומלץ לתת אותו, כי GEPA ו-Auto לומדים ממנו. בלי משוב, המנועים רואים רק את הציונים, ויומן הריצה מציין זאת פעם אחת.
- המפתח `"scores"` הוא אופציונלי. הוא ממפה שמות לציונים בעלי שם. כל ציון בעל שם הוא אובייקט עם `"score"` משלו ועם `"feedback"` אופציונלי. כל מנוע משתמש בכל ציון בעל שם כיעד נפרד.

כל מבנה או מפתח אחר הוא שגיאה. הבדיקה מציגה את החלק השגוי ואת המבנה התקין.

| ערך החזרה | תקין |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | כן |
| `{"score": 0.8}` | כן. אין משוב. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | כן |
| `0.8` | לא. הערך אינו אובייקט. |
| `(0.8, "Close. Missing the date.")` | לא. הערך אינו אובייקט. |
| `{"score": "0.8"}` | לא. הציון הוא מחרוזת. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | לא. ציון בעל שם חייב להיות אובייקט. |
| `{"score": 0.8, "feedbak": "OK"}` | לא. המפתח `feedbak` לא מוכר. |

## כתיבת משוב מועיל

GEPA ו-Auto קוראים את טקסט המשוב כדי לכתוב את הגרסה הבאה. כתוב/י מה לא נכון ואיך נראה פלט נכון.

| משוב חלש      | משוב מועיל                                                                    |
| ------------- | ----------------------------------------------------------------------------- |
| "Wrong."      | "Expected 'billing', got 'other'. The message mentions a charge."             |
| "Bad format." | "The output is not valid JSON. It has a trailing comma after the last field." |

> [!NOTE]
> סוכן הקוד אף פעם לא רואה את קוד המקור של פונקציית הניקוד, את הערכים הצפויים או את הסודות של הריצה. הוא רואה רק את הציונים ואת טקסט המשוב.

## דוגמאות

### התאמה מדויקת

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

`case["expected"]` הוא dict שהמפתח שלו הוא שם עמודת הפלט שלך. קרא/י את [דאטאסטים וחלוקה](/docs/concepts/datasets).

### הרצת הבדיקות של המאגר

ה-`entry` של `[run]` מריץ את הבדיקות.‏ `pytest` יוצא עם קוד יציאה שונה מאפס כשבדיקה נכשלת, וקוד יציאה שונה מאפס מכשיל את המקרה. לכן קובץ העטיפה `run_tests.py` כותב את התוצאה ל-`ASCENT_OUTPUT` ויוצא עם 0. פונקציית הניקוד קוראת את התוצאה הזו.

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

כשה-`entry` של `[run]` נכשל במקרה בדיקה, למקרה הזה אין ציון. אם ה-`entry` לא מתחיל, חורג מהזמן הקצוב או שאות עוצר אותו, או אם `entry` מסוג פונקציה זורק חריגה או יוצא לפני שהוא מחזיר ערך, המשוב הוא `Run failed: …`, ו-Skynet לא קורא לפונקציית הניקוד. ‏`entry` מסוג פקודה נכשל גם כשהוא יוצא עם קוד יציאה שונה מאפס או לא כותב קובץ פלט תקין. לגרסה עם מקרה כזה אין ציון. היא לא יכולה להיות הגרסה הטובה ביותר, לא מגיעה לאף `[outcome] target` ולא פותחת pull request. אם כישלון חייב להיחשב כמספר, תפוס/י את הכישלון בתוכנית שלך, כתוב/כתבי פלט ותן/תני לפונקציית הניקוד לתת לו ציון. קרא/י את [כללי המאגר](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md).

### מודל כשופט

פונקציית הניקוד יכולה לקרוא למודל. ייבא/י את `llm` ממודול העזר `openascent`. הקריאה עוברת דרך שער המודלים של Skynet אל מודל הניקוד של הריצה.

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

פונקציית הניקוד יכולה גם להשתמש בכל לקוח שתואם ל-OpenAI עם משתני הסביבה `OPENAI_BASE_URL`,‏ `APP_API_KEY` ו-`APP_MODEL`. השער מקבל רק את מודל הניקוד של הריצה.

## קריאות למודל וסודות

- קריאות על המפתחות של Skynet נמדדות מול תקציב הריצה. עם מפתח משלך (BYOK), הקריאה הולכת למפתח של הספק שלך, בלי עמלת מודל של Skynet. קרא/י את [חיוב](/docs/concepts/billing).
- פונקציית הניקוד לא מקבלת את הסודות של הריצה כמשתני סביבה. סוד מגיע לפונקציית הניקוד רק דרך טבלת `[services.<name>]` ש-`[metric] services` מפרט, או דרך שרת בקובץ `[metric] mcp`. כתוב/י את הסוד ב-`headers` של הטבלה בצורה `${NAME}`. פונקציית הניקוד שולחת את הבקשות שלה לכתובת ה-URL שב-`ASCENT_SERVICE_<NAME>_URL`. ‏Skynet מוסיף את הסוד מחוץ ל-sandbox.
- Skynet מסתיר את ערכי הסודות בפלט של פונקציית הניקוד וביומנים.

## מגבלות

| פריט                     | ערך                                 |
| ------------------------ | ----------------------------------- |
| זמן קצוב לפונקציית הניקוד | 60 שניות כברירת מחדל, לכל היותר 600 |

## עמודים קשורים

- [דאטאסטים וחלוקה](/docs/concepts/datasets)
- [מנועים](/docs/concepts/engines)
- [הפעלת ריצה](/docs/guides/submit)
