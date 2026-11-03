Die Metrik sagt Skynet, wie gut eine Ausgabe ist. Sie ist eine Python-Funktion in `metric.py`. Jede Engine ruft dieselbe Funktion auf. Diese Seite beschreibt den Vertrag, den Rückgabewert und Beispiele.

## Der Vertrag

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argument   | Was es enthält                                                         |
| ---------- | ---------------------------------------------------------------------- |
| `input`    | Ein Dict mit den Eingabefeldern des Falls                              |
| `output`   | Was Ihre `run(artifact, input)` für den Fall zurückgegeben hat         |
| `expected` | Der erwartete Wert des Falls oder `None`, wenn der Fall keinen hat     |

Nimmt Ihre Funktion nur zwei Argumente, ruft Skynet `score(input, output)` auf.

Bei einem DSPy-Programm passt Skynet den Metrikaufruf von DSPy an dieselbe Funktion an. Sie schreiben eine Metrik für jede Artefaktart. Lesen Sie [So funktioniert die Optimierung](/docs/concepts/optimization).

## Der Rückgabewert

`score` muss ein Paar zurückgeben (ein Tupel oder eine Liste mit zwei Elementen):

1. **Der Score.** Eine Zahl. Der Optimierer versucht, sie zu erhöhen.
2. **Die Zusatzinformationen.** Ein Dict. Es muss einen Schlüssel `"feedback"` mit einem String-Wert haben.

Die Zusatzinformationen können auch einen Schlüssel `"scores"` haben. Er ordnet Namen Zahlen zu, zum Beispiel `{"format": 1.0, "accuracy": 0.5}`.

Hat der Rückgabewert eine andere Form, schlägt die Vertragsprüfung bei **Rückgabewert der Metrik** fehl.

| Rückgabewert                                           | Gültig                                         |
| ------------------------------------------------------ | ---------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Ja                                             |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Ja                                             |
| `0.8`                                                  | Nein. Es gibt keine Zusatzinformationen.       |
| `(0.8, "Close.")`                                      | Nein. Die Zusatzinformationen sind kein Dict.  |
| `(0.8, {})`                                            | Nein. Es gibt keinen Feedback-String.          |
| `("0.8", {"feedback": "OK"})`                          | Nein. Der Score ist ein String.                |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | Nein. Ein benannter Score ist keine Zahl.      |

## Nützliches Feedback schreiben

GEPA, DSPy GEPA und Auto lesen den Feedback-Text, um die nächste Version zu schreiben. Sagen Sie, was falsch ist und wie eine korrekte Ausgabe aussieht.

| Schwaches Feedback | Nützliches Feedback                                                           |
| ------------------ | ----------------------------------------------------------------------------- |
| "Wrong."           | "Expected 'billing', got 'other'. The message mentions a charge."             |
| "Bad format."      | "The output is not valid JSON. It has a trailing comma after the last field." |

> [!NOTE]
> Der Optimierer sieht den Quelltext Ihrer Metrik nie. Bei einem DSPy-Optimierer sieht das Reflexionsmodell nur den Feedback-Text. Ihre erwarteten Werte bleiben bei der Metrik.

## Beispiele

### Exakte Übereinstimmung

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` ist ein Dict mit dem Namen Ihrer Ausgabespalte als Schlüssel. Lesen Sie [Datensätze und Aufteilungen](/docs/concepts/datasets).

### Mehrere Prüfungen

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

### Modell als Bewerter

Die Metrik kann ein Modell aufrufen. Importieren Sie `llm` aus dem Hilfsmodul `skynet`. Ein Aufruf aus der Metrik geht an das Metrikmodell.

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

Die Metrik legt ihr Modell in ihrem eigenen `llm()`-Aufruf fest. Alle vier gezeigten Argumente sind optional. Ohne `model=` verwendet die Metrik das Reflexionsmodell.

## Genauigkeit, Geschwindigkeit und Kosten

Eine Metrik kann die Genauigkeit mit der gemessenen Laufzeit und den Modellkosten verrechnen. Definieren Sie `REWARD_SPEC` auf Modulebene und geben Sie den Wert von `reward` zurück.

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

- `weights` muss `accuracy`, `speed` und `cost` enthalten. Jedes Gewicht ist 0 oder größer, und nicht alle dürfen 0 sein. Skynet skaliert sie so, dass sie zusammen 1 ergeben.
- `speed.reference` ist in Millisekunden angegeben. `cost.reference` ist in US-Dollar angegeben. Beide müssen positiv sein.
- Ohne `accuracy.reference` ist die Genauigkeit ein Score von 0 bis 1. Mit diesem Wert ist die Genauigkeit ein Fehler, und niedriger ist besser.

Skynet misst Zeit und Kosten selbst. Der Ausführungscode kann sie nicht ändern. Kann Skynet die Zeit oder die Kosten nicht messen, erhält dieser Teil den Score 0, und das Feedback weist darauf hin.

Die Metrik kann die Messwerte auch direkt mit `observation()` und `service_cost()` aus `skynet` lesen.

## Limits

| Element                               | Wert                                    |
| ------------------------------------- | --------------------------------------- |
| Timeout der Metrik                    | Standardmäßig 60 Sekunden, höchstens 600 |
| Beispielfälle der Vertragsprüfung     | 3 Fälle                                 |

## Verwandte Seiten

- [Datensätze und Aufteilungen](/docs/concepts/datasets)
- [Engines](/docs/concepts/engines)
- [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit)
