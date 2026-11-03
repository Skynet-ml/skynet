פונקציית המדידה (Metric) אומרת ל-Skynet כמה פלט טוב. הוא פונקציית Python בקובץ `metric.py`. כל מנוע קורא לאותה פונקציה. העמוד הזה מציג את החוזה, את ערך ההחזרה ודוגמאות.

## החוזה

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| ארגומנט    | מה הוא מכיל                                        |
| ---------- | -------------------------------------------------- |
| `input`    | dict עם שדות הקלט של המקרה                         |
| `output`   | מה ש-`run(artifact, input)` שלך החזיר למקרה        |
| `expected` | הערך הצפוי של המקרה, או `None` כשאין למקרה ערך כזה |

אם הפונקציה שלך מקבלת רק שני ארגומנטים, Skynet קורא ל-`score(input, output)`.

בתוכנית DSPy,‏ Skynet מתאים את קריאת פונקציית המדידה של DSPy לאותה פונקציה. כותבים פונקציית מדידה אחת לכל סוגי התוצרים. קרא/י את [איך האופטימיזציה עובדת](/docs/concepts/optimization).

## ערך ההחזרה

`score` חייב להחזיר זוג (tuple או list של שני פריטים):

1. **הציון.** מספר. האופטימייזר מנסה להעלות אותו.
2. **המידע הנלווה.** dict. חייב להיות בו מפתח `"feedback"` עם ערך מסוג מחרוזת.

המידע הנלווה יכול לכלול גם מפתח `"scores"`. הוא ממפה שמות למספרים, למשל `{"format": 1.0, "accuracy": 0.5}`.

אם לערך ההחזרה יש מבנה אחר, בדיקת החוזה נכשלת ב-**ערך ההחזרה של המדד**.

| ערך החזרה                                              | תקין                        |
| ------------------------------------------------------ | --------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | כן                          |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | כן                          |
| `0.8`                                                  | לא. אין מידע נלווה.         |
| `(0.8, "Close.")`                                      | לא. המידע הנלווה אינו dict. |
| `(0.8, {})`                                            | לא. אין מחרוזת משוב.        |
| `("0.8", {"feedback": "OK"})`                          | לא. הציון הוא מחרוזת.       |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | לא. ציון בעל שם אינו מספר.  |

## כתיבת משוב מועיל

GEPA,‏ DSPy GEPA ו-Auto קוראים את טקסט המשוב כדי לכתוב את הגרסה הבאה. כתוב/י מה לא נכון ואיך נראה פלט נכון.

| משוב חלש      | משוב מועיל                                                                    |
| ------------- | ----------------------------------------------------------------------------- |
| "Wrong."      | "Expected 'billing', got 'other'. The message mentions a charge."             |
| "Bad format." | "The output is not valid JSON. It has a trailing comma after the last field." |

> [!NOTE]
> האופטימייזר אף פעם לא רואה את קוד המקור של פונקציית המדידה. עם אופטימייזר של DSPy, מודל האופטימיזציה רואה רק את טקסט המשוב. הערכים הצפויים נשארים אצל פונקציית המדידה.

## דוגמאות

### התאמה מדויקת

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` הוא dict שהמפתח שלו הוא שם עמודת הפלט שלך. קרא/י את [דאטאסטים וחלוקה](/docs/concepts/datasets).

### כמה בדיקות

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

### מודל כשופט

פונקציית המדידה יכולה לקרוא למודל. ייבא/י את `llm` ממודול העזר `skynet`. קריאה מפונקציית המדידה הולכת למודל של פונקציית המדידה.

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

פונקציית המדידה מגדירה את המודל שלה בקריאה שלה ל-`llm()`. כל ארבעת הארגומנטים המוצגים הם אופציונליים. ללא `model=`, פונקציית המדידה משתמשת במודל האופטימיזציה.

## דיוק, מהירות ועלות

פונקציית מדידה יכולה לשלב דיוק עם זמן הריצה הנמדד ועם עלות המודל. הגדר/י `REWARD_SPEC` ברמת המודול והחזר/י את הערך של `reward`.

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

- `weights` חייב לכלול את `accuracy`,‏ `speed` ו-`cost`. כל משקל הוא 0 ומעלה, ולא כולם יכולים להיות 0. ‏Skynet משנה את קנה המידה שלהם כך שהסכום יהיה 1.
- `speed.reference` הוא באלפיות שנייה. `cost.reference` הוא בדולר אמריקאי. שניהם חייבים להיות חיוביים.
- בלי `accuracy.reference`, הדיוק הוא ציון בין 0 ל-1. איתו, הדיוק הוא שגיאה, ונמוך יותר הוא טוב יותר.

Skynet מודד את הזמן ואת העלות בעצמו. קוד ההרצה לא יכול לשנות אותם. אם Skynet לא יכול למדוד את הזמן או את העלות, החלק הזה מקבל ציון 0 והמשוב מציין זאת.

פונקציית המדידה יכולה גם לקרוא את המדידות ישירות בעזרת `observation()` ו-`service_cost()` מ-`skynet`.

## מגבלות

| פריט                      | ערך                                 |
| ------------------------- | ----------------------------------- |
| זמן קצוב לפונקציית המדידה | 60 שניות כברירת מחדל, לכל היותר 600 |
| דוגמאות לבדיקת החוזה      | 3 מקרים                             |

## עמודים קשורים

- [דאטאסטים וחלוקה](/docs/concepts/datasets)
- [מנועים](/docs/concepts/engines)
- [הפעלת ריצה](/docs/guides/submit)
