Skynet verbessert Code, den Sie bereits haben, oder Code, den es für Sie schreibt. Skynet ist Optimize Anything: Jeder Durchlauf optimiert ein Repository. Sie geben ein Repository, eine Reihe von Fällen und einen Scorer vor. Skynet schlägt neue Versionen des Repositorys vor, bewertet jede mit Ihrem Scorer und behält die beste Version.

## Was Skynet optimiert

Jeder Durchlauf optimiert ein Repository. Sie können auf zwei Arten beginnen:

| Start                           | Was passiert                                                                                              |
| ------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Repository verwenden        | Skynet ändert die Dateien in den Pfaden, die Sie freigeben, in einem GitHub-Repository, das Sie verbinden. |
| Neues Repository erstellen  | Sie geben eine Aufgabe und Daten vor. Skynet erstellt ein neues GitHub-Repository in Ihrem Konto und schreibt eine erste Lösung. |

Der Code im Repository kann alles sein: eine Anwendung, ein Dienst, ein Skript oder eine Bibliothek. Er kann jede Bibliothek verwenden.

Jeder Durchlauf nutzt denselben Vertrag. Ihr Scorer definiert `score(repo_path, case)`. Er gibt einen Score und schriftliches Feedback zurück. Der Optimierer liest das Feedback, um die nächste Version zu verbessern.

> [!NOTE]
> Jede Version läuft in einer Sandbox. Der Coding-Agent, der die Versionen schreibt, sieht weder den Quelltext des Scorers noch die erwarteten Werte.

## Ein Produkt, viele Engines

**Auto** ist der Standard für jeden Durchlauf. Es probiert GEPA, AutoResearch, Meta-Harness, AutoSaddler und ShinkaEvolve aus und setzt dann GEPA ab der besten Version fort. Sie können auch eine Engine wählen: GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler oder ShinkaEvolve. Lesen Sie [So funktioniert die Optimierung](/docs/concepts/optimization) und [Engines](/docs/concepts/engines).

## So läuft ein Durchlauf ab

1. Sie beschreiben die Aufgabe und wählen ein Repository oder lassen Skynet eines erstellen.
2. Sie geben die Fälle vor. Skynet teilt sie in Trainings-, Validierungs- und Testmenge auf.
3. Sie wählen einen Optimierer, die Modelle und ein Budget.
4. Sie schreiben den Scorer. Eine Konfigurationsprüfung führt ihn auf der Ausgangsversion aus.
5. Skynet führt die Optimierung aus und zeigt die Ergebnisse.

Skynet wählt Versionen anhand der Validierungsfälle aus. Den endgültigen Score meldet es auf den Testfällen. Die Engines sehen die Testfälle nie.

## Wo Sie anfangen

| Wenn Sie …                                | Lesen Sie                                                |
| ----------------------------------------- | -------------------------------------------------------- |
| Ihren ersten Durchlauf starten möchten    | [Schnellstart](/docs/quickstart)                         |
| verstehen möchten, wie Optimierung funktioniert | [So funktioniert die Optimierung](/docs/concepts/optimization) |
| eine Engine wählen möchten                | [Engines](/docs/concepts/engines)                        |
| Ihre Daten vorbereiten möchten            | [Datensätze und Aufteilungen](/docs/concepts/datasets)   |
| einen Scorer schreiben möchten            | [Metriken und Scorer](/docs/concepts/scorers)            |
| den Assistenten ausfüllen möchten         | [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit) |
| Ihre Ergebnisse lesen möchten             | [Ergebnisse lesen](/docs/guides/results)                 |
| die Kosten verstehen möchten              | [Budgets und Abrechnung](/docs/concepts/billing)         |
| Skynet aus einem Agenten nutzen möchten   | [MCP-Server](/docs/mcp)                                  |
| Skynet aus Code aufrufen möchten          | [API](/docs/api)                                         |
| die Limits prüfen möchten                 | [Limits](/docs/limits)                                   |

> [!TIP]
> Beginnen Sie mit einem kleinen Datensatz und den Standardeinstellungen. Formulieren Sie das Feedback des Scorers konkret. Gutes Feedback gibt dem Optimierer mehr Anhaltspunkte als der Score allein.
