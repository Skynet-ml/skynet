פונקציית הניקוד (Scorer) אומרת ל-Skynet כמה טובה גרסה אחת של המאגר שלך. היא פונקציית Python בקובץ `scorer.py`. כל מנוע קורא לאותה פונקציה. העמוד הזה מציג את החוזה, את ערך ההחזרה ודוגמאות.

## החוזה

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| ארגומנט     | מה הוא מכיל                                                                         |
| ----------- | ----------------------------------------------------------------------------------- |
| `repo_path` | תיקייה שמכילה רק את קובצי המדד שלך. אין בה קובץ אחר של המאגר ואין בה קוד של גרסה |
| `case`      | dict.‏ `case["input"]` מכיל את שדות הקלט.‏ `case["expected"]` מכיל את הערך הצפוי, או `None` |
| `output`    | מה שה-`entry` של `[run]` החזיר עבור המקרה, או `None` כשאין בקובץ `[run]` |

בקובץ `optimize.toml` של המאגר חייב להיות קטע `[run]` או שלב `[target.<name>]`. ה-`entry` של `[run]` מריץ גרסה אחת על מקרה אחד, בתיבת sandbox משלו. ‏Skynet דוחה קובץ בלי אף אחד מהם, כי כל גרסה הייתה מקבלת את אותו ציון.

- ‏`entry` של פונקציה, למשל `entry = "serve.py:serve"`, מקבל את `case["input"]` כארגומנט.‏ `output` הוא ערך ההחזרה שלו.
- ‏`entry` של פקודה, למשל `entry = ["python", "-m", "pytest", "-q"]`, מקבל את הקלט כ-JSON ב-stdin.‏ `output` הוא `{"exit_code": ..., "stdout": "..."}`.

פונקציית הניקוד רצה בתיבת sandbox נפרדת. בתיבה הזו יש רק את קובצי המדד שלך. פונקציית הניקוד לא יכולה לייבא או להריץ את הקוד של הגרסה. היא רואה את הגרסה רק דרך `output`, או דרך `call()` לשלב `[target.<name>]`. את `call` מייבאים עם `from skynet import call`. קרא/י את [איך האופטימיזציה עובדת](/docs/concepts/optimization).

## ערך ההחזרה

`score` חייב להחזיר ציון עם משוב. המבנים האלה תקינים:

1. זוג: מספר, ומחרוזת משוב או dict עם מפתח `"feedback"`.
2. dict עם מספר תחת `"score"` ומחרוזת תחת `"feedback"`.

המשוב חייב להיות מחרוזת שאינה ריקה.

המידע הנלווה יכול לכלול גם מפתח `"scores"`. הוא ממפה שמות לציונים בעלי שם. לכל ציון בעל שם יש ציון ומשוב משלו, למשל `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

המידע הנלווה יכול לכלול גם מפתח `"cases"` עם אותו מבנה. כל רשומה היא התוצאה של מקרה אחד. כל מנוע משתמש בכל מקרה כיעד נפרד.

אם לערך ההחזרה יש מבנה אחר, הבדיקה נכשלת ומציגה את הסיבה.

| ערך החזרה                                                                       | תקין                                   |
| ------------------------------------------------------------------------------- | -------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | כן                                     |
| `(0.8, "Close. Missing the date.")`                                             | כן                                     |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | כן                          |
| `0.8`                                                                           | לא. אין משוב.                          |
| `(0.8, {})`                                                                     | לא. אין מחרוזת משוב.                   |
| `("0.8", {"feedback": "OK"})`                                                   | לא. הציון הוא מחרוזת.                  |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | לא. ציון בעל שם אינו מספר.            |

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

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` הוא dict שהמפתח שלו הוא שם עמודת הפלט שלך. קרא/י את [דאטאסטים וחלוקה](/docs/concepts/datasets).

### הרצת הבדיקות של המאגר

ה-`entry` של `[run]` מריץ את הבדיקות. פונקציית הניקוד קוראת את קוד היציאה ואת הפלט של הבדיקות.

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": output["stdout"][-2000:]}
```

כשה-`entry` של `[run]` נכשל במקרה בדיקה, למקרה הזה אין ציון. אם ה-`entry` לא מתחיל, חורג מהזמן הקצוב או שאות עוצר אותו, המשוב הוא `Run failed: …`, ו-Skynet לא קורא לפונקציית הניקוד. קוד יציאה שונה מאפס אינו כישלון: פונקציית הניקוד מקבלת אותו ב-`output`. לגרסה עם מקרה כזה אין ציון. היא לא יכולה להיות הגרסה הטובה ביותר, לא מגיעה לאף `[outcome] target` ולא פותחת pull request. אם כישלון חייב להיחשב כמספר, תפוס/י את הכישלון בתוכנית שלך, כתוב/כתבי פלט ותן/תני לפונקציית הניקוד לתת לו ציון. קרא/י את [כללי המאגר](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md).

### מודל כשופט

פונקציית הניקוד יכולה לקרוא למודל. ייבא/י את `llm` ממודול העזר `skynet`. הקריאה עוברת דרך שער המודלים של Skynet אל מודל הניקוד של הריצה.

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from skynet import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

פונקציית הניקוד יכולה גם להשתמש בכל לקוח שתואם ל-OpenAI עם משתני הסביבה `OPENAI_BASE_URL`,‏ `APP_API_KEY` ו-`APP_MODEL`. השער מקבל רק את מודל הניקוד של הריצה.

## קריאות למודל וסודות

- קריאות על המפתחות של Skynet נמדדות מול תקציב הריצה. עם מפתח משלך (BYOK), הקריאה הולכת למפתח של הספק שלך, בלי עמלת מודל של Skynet. קרא/י את [חיוב](/docs/concepts/billing).
- פונקציית הניקוד לא מקבלת את הסודות של הריצה כמשתני סביבה. סוד מגיע לפונקציית הניקוד רק דרך טבלת `[services.<name>]` ש-`[metric] services` מפרט. כתוב/י את הסוד ב-`headers` של הטבלה בצורה `${NAME}`. פונקציית הניקוד שולחת את הבקשות שלה לכתובת ה-URL שב-`SKYNET_SERVICE_<NAME>_URL`. ‏Skynet מוסיף את הסוד מחוץ ל-sandbox.
- Skynet מסתיר את ערכי הסודות בפלט של פונקציית הניקוד וביומנים.

## מגבלות

| פריט                     | ערך                                 |
| ------------------------ | ----------------------------------- |
| זמן קצוב לפונקציית הניקוד | 60 שניות כברירת מחדל, לכל היותר 600 |

## עמודים קשורים

- [דאטאסטים וחלוקה](/docs/concepts/datasets)
- [מנועים](/docs/concepts/engines)
- [הפעלת ריצה](/docs/guides/submit)
