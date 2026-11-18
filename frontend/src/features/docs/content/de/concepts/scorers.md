Der Scorer sagt Skynet, wie gut eine Version Ihres Repositorys ist. Er ist eine Python-Funktion in `scorer.py`. Jede Engine ruft dieselbe Funktion auf. Diese Seite beschreibt den Vertrag, den Rückgabewert und Beispiele.

## Der Vertrag

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argument    | Was es enthält                                                                   |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Ein Ordner, der nur Ihre Metrikdateien enthält. Er enthält keine andere Datei des Repositorys und keinen Code der Version |
| `case`      | Ein Dict. `case["input"]` enthält die Eingabefelder. `case["expected"]` enthält den erwarteten Wert oder `None` |
| `output`    | Was der `[run]`-Eintrag für den Fall zurückgegeben hat, oder `None`, wenn die Datei kein `[run]` hat |

Die Datei `optimize.toml` des Repositorys muss einen Abschnitt `[run]` oder eine Stufe `[target.<name>]` haben. Der `entry` von `[run]` führt eine Version auf einem Fall aus, in einer eigenen Sandbox-Box. Skynet lehnt eine Datei ohne beides ab, weil sonst jede Version dieselbe Bewertung erhält.

- Ein Funktionseintrag, zum Beispiel `entry = "serve.py:serve"`, erhält `case["input"]` als Argument. `output` ist sein Rückgabewert.
- Ein Befehlseintrag, zum Beispiel `entry = ["python", "-m", "pytest", "-q"]`, erhält die Eingabe als JSON auf stdin. `output` ist `{"exit_code": ..., "stdout": "..."}`.

Der Scorer läuft in einer separaten Sandbox-Box. Diese Box enthält nur Ihre Metrikdateien. Der Scorer kann den Code der Version weder importieren noch ausführen. Er sieht die Version nur über `output` oder über `call()` an eine Stufe `[target.<name>]`. Importieren Sie `call` mit `from skynet import call`. Lesen Sie [So funktioniert die Optimierung](/docs/concepts/optimization).

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

`case["expected"]` ist ein Dict, dessen Schlüssel der Name Ihrer Ausgabespalte ist. Lesen Sie [Datensätze und Aufteilungen](/docs/concepts/datasets).

### Die Tests des Repositorys ausführen

Der `[run]`-Eintrag führt die Tests aus. Der Scorer liest den Exit-Code und die Testausgabe.

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

Wenn der `[run]`-Eintrag bei einem Fall fehlschlägt, hat dieser Fall keine Bewertung. Wenn der Eintrag nicht startet, das Zeitlimit überschreitet oder ein Signal ihn stoppt, ist das Feedback `Run failed: …`, und Skynet ruft den Scorer nicht auf. Ein Exit-Code ungleich null ist kein Fehlschlag: Der Scorer erhält ihn in `output`. Eine Version mit einem solchen Fall hat keine Bewertung. Sie kann nicht die beste Version sein, erreicht kein `[outcome] target` und öffnet keinen Pull Request. Wenn ein Fehlschlag als Zahl zählen soll, fangen Sie den Fehlschlag in Ihrem Programm ab, schreiben Sie eine Ausgabe und lassen Sie den Scorer sie bewerten. Lesen Sie die [Repository-Regeln](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md).

### Modell als Bewerter

Der Scorer kann ein Modell aufrufen. Importieren Sie `llm` aus dem Hilfsmodul `skynet`. Der Aufruf geht über das Modell-Gateway von Skynet an das Scorer-Modell des Durchlaufs.

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

Der Scorer kann auch jeden OpenAI-kompatiblen Client mit den Umgebungsvariablen `OPENAI_BASE_URL`, `APP_API_KEY` und `APP_MODEL` verwenden. Das Gateway akzeptiert nur das Scorer-Modell des Durchlaufs.

## Modellaufrufe und Secrets

- Aufrufe über die Schlüssel von Skynet werden auf das Laufbudget angerechnet. Mit Ihrem eigenen Schlüssel (BYOK) geht der Aufruf an Ihren Anbieterschlüssel, ohne Modellgebühr von Skynet. Lesen Sie [Abrechnung](/docs/concepts/billing).
- Der Scorer erhält keine Secrets des Durchlaufs als Umgebungsvariablen. Ein Secret erreicht den Scorer nur über eine Tabelle `[services.<name>]`, die `[metric] services` nennt. Schreiben Sie das Secret als `${NAME}` in die `headers` der Tabelle. Der Scorer sendet seine Anfragen an die URL in `SKYNET_SERVICE_<NAME>_URL`. Skynet fügt das Secret außerhalb der Sandbox hinzu.
- Skynet verbirgt Secret-Werte in der Ausgabe des Scorers und in den Logs.

## Limits

| Element            | Wert                                     |
| ------------------ | ---------------------------------------- |
| Timeout des Scorers | Standardmäßig 60 Sekunden, höchstens 600 |

## Verwandte Seiten

- [Datensätze und Aufteilungen](/docs/concepts/datasets)
- [Engines](/docs/concepts/engines)
- [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit)
