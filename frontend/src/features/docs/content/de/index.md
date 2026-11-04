Skynet verbessert ein Artefakt, das Sie bereits haben. Sie geben eine Ausgangsversion, eine Reihe von Fällen und eine Metrik vor. Skynet schlägt neue Versionen vor, führt jede davon auf Ihren Fällen aus, bewertet die Ausgabe mit Ihrer Metrik und behält die beste Version.

## Was Skynet optimiert

Skynet arbeitet mit zwei Artefaktarten:

| Artefaktart   | Was sich ändert                                                   |
| ------------- | ----------------------------------------------------------------- |
| Repository | Die Dateien in den Pfaden, die Sie freigeben |
| DSPy-Programm | Die Anweisungen der Predictors in einem `dspy.Module`             |

Jeder Durchlauf nutzt denselben Vertrag. Ihr Ausführungscode definiert `run(artifact, input)`. Ihre Metrik definiert `score(input, output, expected=None)`. Die Metrik gibt einen Score und schriftliches Feedback zurück. Der Optimierer liest das Feedback, um die nächste Version zu verbessern.

> [!NOTE]
> Ihr Ausführungscode und Ihre Metrik laufen in einer Sandbox, in getrennten Prozessen. Der Ausführungscode sieht weder den Quelltext der Metrik noch die erwarteten Werte.

## Ein Produkt, viele Engines

Skynet ist Optimize Anything. Die Artefaktart bestimmt, welche Engines daran arbeiten können:

- Repositorys nutzen die nativen Engines: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler und ShinkaEvolve.
- DSPy-Programm-Artefakte nutzen die DSPy-Optimierer: DSPy GEPA, DSPy MIPROv2 und DSPy BootstrapFewShot.

Bei einem DSPy-Programm verwendet **Auto** DSPy GEPA. Bei einem Repository wählen Sie eine native Engine. Lesen Sie [So funktioniert die Optimierung](/docs/concepts/optimization) und [Engines](/docs/concepts/engines).

## So läuft ein Durchlauf ab

1. Sie beschreiben die Aufgabe und legen die Ausgangsversion fest.
2. Sie geben die Fälle vor. Skynet teilt sie in Trainings-, Validierungs- und Testmenge auf.
3. Sie wählen einen Optimierer, die Modelle und ein Budget.
4. Sie schreiben den Ausführungscode und die Metrik. Eine Vertragsprüfung führt beide auf Beispielfällen aus.
5. Skynet führt die Optimierung aus und zeigt die Ergebnisse.

Skynet wählt Versionen anhand der Validierungsfälle aus. Den endgültigen Score meldet es auf den Testfällen. Die Engines sehen die Testfälle nie.

## Wo Sie anfangen

| Wenn Sie …                                | Lesen Sie                                                |
| ----------------------------------------- | -------------------------------------------------------- |
| Ihren ersten Durchlauf starten möchten    | [Schnellstart](/docs/quickstart)                         |
| verstehen möchten, wie Optimierung funktioniert | [So funktioniert die Optimierung](/docs/concepts/optimization) |
| eine Engine wählen möchten                | [Engines](/docs/concepts/engines)                        |
| Ihre Daten vorbereiten möchten            | [Datensätze und Aufteilungen](/docs/concepts/datasets)   |
| eine Metrik schreiben möchten             | [Metriken und Scorer](/docs/concepts/scorers)            |
| den Assistenten ausfüllen möchten         | [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit) |
| Ihre Ergebnisse lesen möchten             | [Ergebnisse lesen](/docs/guides/results)                 |
| die Kosten verstehen möchten              | [Budgets und Abrechnung](/docs/concepts/billing)         |
| Skynet aus einem Agenten nutzen möchten   | [MCP-Server](/docs/mcp)                                  |
| Skynet aus Code aufrufen möchten          | [API](/docs/api)                                         |
| die Limits prüfen möchten                 | [Limits](/docs/limits)                                   |

> [!TIP]
> Beginnen Sie mit einem kleinen Datensatz und den Standardeinstellungen. Formulieren Sie das Feedback der Metrik konkret. Gutes Feedback gibt dem Optimierer mehr Anhaltspunkte als der Score allein.
