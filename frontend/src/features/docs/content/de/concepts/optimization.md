Skynet ist Optimize Anything: Jeder Durchlauf optimiert ein Repository. Eine Engine schlägt neue Versionen des Repositorys vor, Ihr Scorer bewertet jede Version, und Skynet behält die beste.

## Der gemeinsame Vertrag

Jeder Durchlauf hat ein Repository und einen Scorer. Der Scorer ist eine Python-Funktion, die den Pfad einer Version des Repositorys und einen Fall erhält.

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

- `repo_path` ist der Ordner einer Version des Repositorys, nachdem der Setup-Befehl gelaufen ist.
- `case` ist ein Dict. `case["input"]` enthält die Eingabefelder eines Falls. `case["expected"]` enthält den erwarteten Wert oder `None`, wenn der Fall keinen hat.
- `score` gibt ein Paar zurück: eine Zahl und ein Dict mit einem `"feedback"`-String.

Das Repository hat im Stammverzeichnis eine Datei `serve.py` mit `def serve(input)`. Der Scorer kann sie aufrufen, und Skynet stellt damit nach dem Durchlauf die beste Version bereit.

> [!NOTE]
> Der Coding-Agent, der die Versionen schreibt, sieht weder den Quelltext des Scorers noch die erwarteten Werte oder die Secrets des Durchlaufs.

Den vollständigen Vertrag des Scorers finden Sie unter [Metriken und Scorer](/docs/concepts/scorers).

## Modellaufrufe aus Ihrem Code

Ihr Repository-Code und Ihr Scorer können über das Modell-Gateway von Skynet ein Modell aufrufen. Die Sandbox stellt in Umgebungsvariablen einen OpenAI-kompatiblen Endpunkt bereit:

| Variable             | Was sie enthält                                                  |
| -------------------- | ---------------------------------------------------------------- |
| `OPENAI_BASE_URL`    | Die URL des Gateways. `APP_GATEWAY_URL` enthält denselben Wert.  |
| `APP_API_KEY`        | Ein Token nur für diesen Durchlauf. Es ist kein Anbieterschlüssel. |
| `APP_MODEL`          | Der Modellname zum Senden. Es ist das Scorer-Modell des Durchlaufs. |
| `ANTHROPIC_BASE_URL` | Die URL des Gateways für Anthropic-SDKs.                         |

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

- Aufrufe über die Schlüssel von Skynet werden auf das Laufbudget angerechnet.
- Mit Ihrem eigenen Schlüssel (BYOK) geht der Aufruf an Ihren Anbieterschlüssel. Skynet berechnet keine Modellgebühr.
- Das Gateway lehnt Anfragen für jedes andere Modell ab.
- Die Secrets des Durchlaufs erreichen Ihren Code und Ihren Scorer als Umgebungsvariablen. Der Variablenname ist der Name des Secrets. Skynet verbirgt Secret-Werte in der Ausgabe.

Die Sandbox hat keinen anderen Netzwerkzugang.

## Engines

Jede Engine arbeitet mit Repositorys:

- **Auto** ist der Standard. Es probiert mehrere Engines aus und setzt dann GEPA ab der besten Version fort.
- **GEPA** entwickelt Versionen durch Reflexion weiter und behält eine Pareto-Front.
- **Best-of-N** fragt das Reflexionsmodell nach unabhängigen Vorschlägen und behält den besten.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** und **ShinkaEvolve** schlagen Versionen mit einem Coding-Agenten vor.

Die Engines ändern nur die Dateien in den Pfaden, die Sie freigeben. Jede Engine und ihre Einstellungen beschreibt [Engines](/docs/concepts/engines).

## Auto

**Auto** ist die Standardwahl im Assistenten, für jeden Durchlauf.

1. Auto erkundet mit fünf Engines: GEPA, AutoResearch, Meta-Harness, AutoSaddler und ShinkaEvolve.
2. Es wählt die beste Version, die sie gefunden haben.
3. Es setzt GEPA ab dieser Version fort, bis das Budget aufgebraucht ist.

Auto liest den Feedback-Text Ihres Scorers.

## Woher die Ausgangsversion kommt

| Ihre Situation                                 | Verwenden Sie                                                                                      |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Sie haben Code in einem GitHub-Repository      | **Repository verwenden**. Der Code im gewählten Branch oder Commit ist der Ausgangspunkt.      |
| Sie haben nur eine Aufgabe und Daten           | **Neues Repository erstellen**. Skynet schreibt eine erste Lösung. Das ist der Ausgangspunkt.  |

> [!WARNING]
> GEPA und Auto lernen aus dem Feedback-Text. Ist Ihr Feedback immer derselbe String, haben diese Optimierer weniger Informationen zur Verfügung.

## Verwandte Seiten

- [Engines](/docs/concepts/engines)
- [Datensätze und Aufteilungen](/docs/concepts/datasets)
- [Metriken und Scorer](/docs/concepts/scorers)
- [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit)
