Skynet ist Optimize Anything: ein Produkt, das ein DSPy-Programm oder ein Repository anhand Ihrer Metrik verbessert. Die Artefaktart entscheidet, welche Engines damit arbeiten können.

## Der gemeinsame Vertrag

Jeder Durchlauf hat zwei Dateien. `run.py` führt das Artefakt auf einem Fall aus. `metric.py` bewertet die Ausgabe.

```python tab="run.py"
from skynet import llm


def run(artifact, input):
    return llm(artifact, input=input)
```

```python tab="metric.py"
def score(input, output, expected=None):
    ok = output == expected
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `artifact` ist die aktuelle Version, die der Optimierer vorschlägt.
- `input` ist ein Dict mit den Eingabefeldern eines Falls.
- `expected` ist der erwartete Wert des Falls oder `None`, wenn der Fall keinen hat.
- `score` gibt ein Paar zurück: eine Zahl und ein Dict mit einem `"feedback"`-String.

Das Hilfsmodul `skynet` stellt `llm`, `Image` und `log` bereit. Ein Aufruf von `llm` aus dem Ausführungscode geht an das Aufgabenmodell. Ein Aufruf von `llm` aus der Metrik geht an das Metrikmodell.

> [!NOTE]
> Der Ausführungscode läuft in einem eigenen Prozess in der Sandbox. Er sieht weder den Quelltext der Metrik noch den erwarteten Wert oder den Metrikschlüssel.

Den vollständigen Metrikvertrag finden Sie unter [Metriken und Scorer](/docs/concepts/scorers).

## Artefaktarten und Engines

| Artefaktart   | Engines                                                                      |
| ------------- | ---------------------------------------------------------------------------- |
| Repository | GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve |
| DSPy-Programm | Auto, DSPy GEPA, DSPy MIPROv2, DSPy BootstrapFewShot                         |

Es gibt zwei Familien von Engines:

- Die **nativen Engines** behandeln das Artefakt als Black Box. Sie arbeiten mit Repositorys.
- Die **DSPy-Optimierer** arbeiten innerhalb eines DSPy-Programms. Sie arbeiten nur mit DSPy-Programm-Artefakten.

DSPy MIPROv2 und DSPy BootstrapFewShot benötigen Paare (Fälle mit erwarteten Ausgaben). Mit einer einzelnen Eingabe kann ein DSPy-Programm nur Auto und DSPy GEPA verwenden.

## Repositorys

Die nativen Engines ändern die Dateien in den Pfaden, die Sie freigeben. Ihre Metrik bewertet jede Version des Repositorys.

Die sechs nativen Engines verwenden unterschiedliche Suchverfahren:

- **GEPA** entwickelt Versionen durch Reflexion weiter und behält eine Pareto-Front.
- **Best-of-N** fragt das Reflexionsmodell nach unabhängigen Vorschlägen und behält den besten.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** und **ShinkaEvolve** schlagen Versionen mit einem Coding-Agenten vor.

Jede Engine und ihre Einstellungen beschreibt [Engines](/docs/concepts/engines).

## DSPy-Programm-Artefakte

Ein DSPy-Programm ist eine Artefaktart. Ihre `run.py` baut auf Modulebene ein `dspy.Module` und ruft es innerhalb von `run` auf. Enthält die Datei mehr als ein Modul, nennen Sie das zu optimierende Modul `program`.

Das passiert, wenn ein DSPy-Optimierer läuft:

1. Skynet findet Ihr Modul und legt Ihre Funktion `run` darum. Jeder Fall durchläuft Ihre eigene `run`.
2. Der DSPy-Optimierer kompiliert das Programm auf den Trainingsfällen.
3. Er wählt anhand der Validierungsfälle aus.
4. Der Gewinner ist der Anweisungstext jedes Predictors plus der vollständige kompilierte Zustand des Moduls. Der Zustand enthält die Few-Shot-Demos.

Die endgültigen Scores messen dasselbe Programm, das Skynet speichert.

| Optimierer            | Liest Feedback-Text |
| --------------------- | ------------------- |
| DSPy GEPA             | Ja                  |
| DSPy MIPROv2          | Nein, nur den Score |
| DSPy BootstrapFewShot | Nein, nur den Score |

DSPy BootstrapFewShot verwendet kein Reflexionsmodell.

> [!TIP]
> Das DSPy-Reflexionsmodell sieht nur den Feedback-Text, den Ihre Metrik zurückgibt. Den Quelltext der Metrik sieht es nie.

## Auto

**Auto** ist die Standardwahl im Assistenten.

- **Repository**: Auto ist nicht verfügbar. Wählen Sie eine native Engine.
- Bei einem DSPy-Programm führt Auto DSPy GEPA aus.

Auto liest den Feedback-Text Ihrer Metrik.

## Welche Engine Sie wählen

| Ihre Situation                                                   | Verwenden Sie                                                       |
| ---------------------------------------------------------------- | ------------------------------------------------------------------- |
| Sie haben Code in einem GitHub-Repository | Ein Repository. Wählen Sie eine native Engine. |
| Sie haben ein DSPy-Programm                                      | Ein DSPy-Programm-Artefakt. Beginnen Sie mit Auto.                  |
| Sie möchten Few-Shot-Demos aus Ihren Daten auswählen lassen      | Ein DSPy-Programm-Artefakt mit DSPy BootstrapFewShot oder DSPy MIPROv2 |

> [!WARNING]
> GEPA, DSPy GEPA und Auto lernen aus dem Feedback-Text. Ist Ihr Feedback immer derselbe String, haben diese Optimierer weniger Informationen zur Verfügung.

## Verwandte Seiten

- [Engines](/docs/concepts/engines)
- [Datensätze und Aufteilungen](/docs/concepts/datasets)
- [Metriken und Scorer](/docs/concepts/scorers)
- [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit)
