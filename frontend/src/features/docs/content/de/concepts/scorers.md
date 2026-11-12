Der Scorer sagt Skynet, wie gut eine Version Ihres Repositorys ist. Er ist eine Python-Funktion in `scorer.py`. Jede Engine ruft dieselbe Funktion auf. Diese Seite beschreibt den Vertrag, den Rückgabewert und Beispiele.

## Der Vertrag

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| Argument    | Was es enthält                                                                   |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Der Ordner einer Version des Repositorys, nachdem der Setup-Befehl gelaufen ist  |
| `case`      | Ein Dict. `case["input"]` enthält die Eingabefelder. `case["expected"]` enthält den erwarteten Wert oder `None` |

Nimmt Ihre Funktion nur ein Argument, ruft Skynet `score(repo_path)` auf. Verwenden Sie diese Form, wenn der Scorer eigene Tests ausführt und keine Fälle braucht.

Der Scorer läuft in der Sandbox, im Ordner der Version. Er kann Code aus dem Repository importieren, einen Befehl ausführen oder `serve()` aus `serve.py` aufrufen. Lesen Sie [So funktioniert die Optimierung](/docs/concepts/optimization).

## Der Rückgabewert

`score` muss einen Score mit Feedback zurückgeben. Diese Formen sind gültig:

1. Ein Paar: eine Zahl und ein Feedback-String oder ein Dict mit einem Schlüssel `"feedback"`.
2. Ein Dict mit einer Zahl unter `"score"` und einem String unter `"feedback"`.

Das Feedback muss ein String sein, der nicht leer ist.

Die Zusatzinformationen können auch einen Schlüssel `"scores"` haben. Er ordnet Namen benannten Scores zu. Jeder benannte Score hat einen eigenen Score und ein eigenes Feedback, zum Beispiel `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

Die Zusatzinformationen können auch einen Schlüssel `"cases"` mit derselben Form haben. Jeder Eintrag ist das Ergebnis eines Falls. Jede Engine verwendet jeden Fall als eigenes Ziel.

Hat der Rückgabewert eine andere Form, schlägt die Prüfung fehl und zeigt den Grund.

| Rückgabewert                                                                    | Gültig                                 |
| ------------------------------------------------------------------------------- | -------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | Ja                                     |
| `(0.8, "Close. Missing the date.")`                                             | Ja                                     |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | Ja                          |
| `0.8`                                                                           | Nein. Es gibt kein Feedback.           |
| `(0.8, {})`                                                                     | Nein. Es gibt keinen Feedback-String.  |
| `("0.8", {"feedback": "OK"})`                                                   | Nein. Der Score ist ein String.        |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | Nein. Ein benannter Score ist keine Zahl. |

## Nützliches Feedback schreiben

GEPA und Auto lesen den Feedback-Text, um die nächste Version zu schreiben. Sagen Sie, was falsch ist und wie eine korrekte Ausgabe aussieht.

| Schwaches Feedback | Nützliches Feedback                                                           |
| ------------------ | ----------------------------------------------------------------------------- |
| "Wrong."           | "Expected 'billing', got 'other'. The message mentions a charge."             |
| "Bad format."      | "The output is not valid JSON. It has a trailing comma after the last field." |

> [!NOTE]
> Der Coding-Agent sieht nie den Quelltext Ihres Scorers, die erwarteten Werte oder die Secrets des Durchlaufs. Er sieht nur die Scores und den Feedback-Text.

## Beispiele

### Exakte Übereinstimmung

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` ist ein Dict, dessen Schlüssel der Name Ihrer Ausgabespalte ist. Lesen Sie [Datensätze und Aufteilungen](/docs/concepts/datasets).

### Die Tests des Repositorys ausführen

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### Modell als Bewerter

Der Scorer kann ein Modell aufrufen. Importieren Sie `llm` aus dem Hilfsmodul `skynet`. Der Aufruf geht über das Modell-Gateway von Skynet an das Scorer-Modell des Durchlaufs.

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
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

Der Scorer kann auch jeden OpenAI-kompatiblen Client mit den Umgebungsvariablen `OPENAI_BASE_URL`, `APP_API_KEY` und `APP_MODEL` verwenden. Das Gateway akzeptiert nur das Scorer-Modell des Durchlaufs.

## Modellaufrufe und Secrets

- Aufrufe über die Schlüssel von Skynet werden auf das Laufbudget angerechnet. Mit Ihrem eigenen Schlüssel (BYOK) geht der Aufruf an Ihren Anbieterschlüssel, ohne Modellgebühr von Skynet. Lesen Sie [Abrechnung](/docs/concepts/billing).
- Die Secrets des Durchlaufs erreichen den Scorer als Umgebungsvariablen. Der Variablenname ist der Name des Secrets, zum Beispiel `os.environ["SEARCH_API_KEY"]`.
- Skynet verbirgt Secret-Werte in der Ausgabe des Scorers und in den Logs.

## Limits

| Element            | Wert                                     |
| ------------------ | ---------------------------------------- |
| Timeout des Scorers | Standardmäßig 60 Sekunden, höchstens 600 |

## Verwandte Seiten

- [Datensätze und Aufteilungen](/docs/concepts/datasets)
- [Engines](/docs/concepts/engines)
- [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit)
