פונקציית הניקוד (Scorer) אומרת ל-Skynet כמה טובה גרסה אחת של המאגר שלך. היא פונקציית Python בקובץ `scorer.py`. כל מנוע קורא לאותה פונקציה. העמוד הזה מציג את החוזה, את ערך ההחזרה ודוגמאות.

## החוזה

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| ארגומנט     | מה הוא מכיל                                                                         |
| ----------- | ----------------------------------------------------------------------------------- |
| `repo_path` | התיקייה של גרסה אחת של המאגר, אחרי שפקודת ההתקנה רצה                                |
| `case`      | dict.‏ `case["input"]` מכיל את שדות הקלט.‏ `case["expected"]` מכיל את הערך הצפוי, או `None` |

אם הפונקציה שלך מקבלת רק ארגומנט אחד, Skynet קורא ל-`score(repo_path)`. השתמש/י בצורה הזו כשפונקציית הניקוד מריצה בדיקות משלה ולא צריכה מקרים.

פונקציית הניקוד רצה ב-sandbox, בתיקייה של הגרסה. היא יכולה לייבא קוד מהמאגר, להריץ פקודה או לקרוא ל-`serve()` מ-`serve.py`. קרא/י את [איך האופטימיזציה עובדת](/docs/concepts/optimization).

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

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` הוא dict שהמפתח שלו הוא שם עמודת הפלט שלך. קרא/י את [דאטאסטים וחלוקה](/docs/concepts/datasets).

### הרצת הבדיקות של המאגר

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
```

### מודל כשופט

פונקציית הניקוד יכולה לקרוא למודל. ייבא/י את `llm` ממודול העזר `skynet`. הקריאה עוברת דרך שער המודלים של Skynet אל מודל הניקוד של הריצה.

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
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

פונקציית הניקוד יכולה גם להשתמש בכל לקוח שתואם ל-OpenAI עם משתני הסביבה `OPENAI_BASE_URL`,‏ `APP_API_KEY` ו-`APP_MODEL`. השער מקבל רק את מודל הניקוד של הריצה.

## קריאות למודל וסודות

- קריאות על המפתחות של Skynet נמדדות מול תקציב הריצה. עם מפתח משלך (BYOK), הקריאה הולכת למפתח של הספק שלך, בלי עמלת מודל של Skynet. קרא/י את [חיוב](/docs/concepts/billing).
- הסודות של הריצה מגיעים לפונקציית הניקוד כמשתני סביבה. שם המשתנה הוא שם הסוד, למשל `os.environ["SEARCH_API_KEY"]`.
- Skynet מסתיר את ערכי הסודות בפלט של פונקציית הניקוד וביומנים.

## מגבלות

| פריט                     | ערך                                 |
| ------------------------ | ----------------------------------- |
| זמן קצוב לפונקציית הניקוד | 60 שניות כברירת מחדל, לכל היותר 600 |

## עמודים קשורים

- [דאטאסטים וחלוקה](/docs/concepts/datasets)
- [מנועים](/docs/concepts/engines)
- [הפעלת ריצה](/docs/guides/submit)
