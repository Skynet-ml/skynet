Eine Engine ist die Suchmethode, die neue Versionen Ihres Repositorys vorschlägt. Skynet hat sechs native Engines und **Auto**. Jede Engine arbeitet mit Repositorys. Diese Seite erklärt, was jede tut, wann Sie sie wählen und welche Einstellungen sie hat.

Lesen Sie zuerst [So funktioniert die Optimierung](/docs/concepts/optimization), wenn Sie nicht wissen, welche Engine zu Ihrer Aufgabe passt.

## Schnellvergleich

| Engine       | Einstellungen für den Coding-Agenten         | Einstellung „Max. Iterationen“ |
| ------------ | -------------------------------------------- | ------------------------------ |
| Auto         | Nein                                         | Nein                           |
| GEPA         | Nein                                         | Nein                           |
| Best-of-N    | Nein                                         | Nein                           |
| AutoResearch | Ja                                           | Nein                           |
| Meta-Harness | Ja                                           | Ja                             |
| AutoSaddler  | Ja                                           | Ja                             |
| ShinkaEvolve | Nein. Es hat eine eigene Editor-Einstellung. | Ja                             |

Der Assistent kennzeichnet GEPA und Auto als Optimierer, die den Feedback-Text Ihres Scorers verwenden.

## Auto

Auto ist die Standardwahl für jeden Durchlauf. Es arbeitet in zwei Phasen:

1. Es erkundet mit GEPA, AutoResearch, Meta-Harness, AutoSaddler und ShinkaEvolve.
2. Es setzt GEPA ab der besten Version fort, die die Erkundung gefunden hat.

**Wählen Sie sie, wenn** Sie nicht wissen, welche Engine passt. Hier fangen Sie am besten an.

## GEPA

„Reflektierende Evolution mit einer Pareto-Front von Versionen.“ Das Reflexionsmodell liest Score und Feedback jedes Falls und schreibt eine bessere Version. GEPA behält eine Front von Versionen, von denen jede bei einigen Fällen die beste ist.

**Wählen Sie sie, wenn** Ihr Scorer konkretes Feedback gibt und Sie eine starke einzelne Engine möchten.

## Best-of-N

„Unabhängige Vorschläge des Reflexionsmodells; der beste wird behalten.“ Jeder Vorschlag ist von den anderen unabhängig. Skynet bewertet die besten Vorschläge erneut auf den Validierungsfällen.

**Wählen Sie sie, wenn** Sie eine einfache Basislinie zum Vergleich mit anderen Engines möchten.

## AutoResearch

Ein Coding-Agent führt Forschungsrunden aus. Er bearbeitet, bewertet und behält, was hilft, bis das Budget aufgebraucht ist.

**Wählen Sie sie, wenn** eine gute Änderung mehrere Untersuchungsschritte erfordert.

## Meta-Harness

Ein Coding-Agent schlägt in jeder Iteration eine Reihe von Kandidaten vor. Er nutzt den gesamten Verlauf der Kandidaten und Scores.

**Wählen Sie sie, wenn** Sie einen Agenten möchten, der aus dem Verlauf aller früheren Versuche lernt. Mit **Kandidaten pro Iteration** legen Sie die Größe der Reihe fest.

## AutoSaddler

Ein Coding-Agent liest das Feedback Ihres Scorers und patcht die Version. Skynet behält nur Patches, die ein neuer Bewertungslauf bestätigt.

**Wählen Sie sie, wenn** das Feedback Ihres Scorers auf klare, lokale Fehler zeigt.

## ShinkaEvolve

„Entwickelt viele Versionen parallel auf Inseln und lernt, welche Modelle sie am meisten verbessern.“ Es ist eine evolutionäre Suche über eine Population von Kandidaten, mit Mutationen und Kreuzungen, die ein Modell schreibt.

**Wählen Sie sie, wenn** Sie breit erkunden möchten und ein größeres Budget haben.

## Einstellungen

### Budget

| Einstellung             | Standard | Bereich                                                        |
| ----------------------- | -------- | -------------------------------------------------------------- |
| Max. Bewertungsläufe    | 200      | 1 bis 100000                              |
| Stoppen bei Bewertung   | Aus      | 0 oder mehr                                                    |
| Max. Iterationen        | Aus      | 1 bis 1000. Nur Meta-Harness, AutoSaddler und ShinkaEvolve.    |

Ein Bewertungslauf ist ein Aufruf Ihres Scorers auf einem Fall. Die abschließende Neubewertung der Ausgangsversion und des Gewinners zählt nicht gegen **Max. Bewertungsläufe**.

### Einstellungen für den Coding-Agenten

Diese Einstellungen gelten für AutoResearch, Meta-Harness und AutoSaddler.

| Einstellung                     | Standard | Bereich                                            |
| ------------------------------- | -------- | -------------------------------------------------- |
| Coding-Agent                    | Codex    | Pi, Codex, OpenCode, OpenCode V2, Prime Agent, Aider, OpenHands, OpenHands SDK, Antigravity SDK, Goose, Hermes Agent, eve, DeerFlow, Cline, Code Puppy, Eigener Befehl   |
| Kandidaten pro Iteration        | 3        | 1 bis 8. |
| Max. Tool-Aufrufe pro Kandidat  | 100      | 1 bis 1000                                         |

### Einstellungen für ShinkaEvolve

| Einstellung                                         | Standard                                                         |
| --------------------------------------------------- | ---------------------------------------------------------------- |
| Wie neue Versionen entstehen                        | Einzelner Modellaufruf. Die andere Option ist „Coding-Agent (Pi)“. |
| Inseln                                              | 2 (1 bis 8)                                                      |
| Migrationsintervall                                 | 10                                                               |
| Elternauswahl                                       | Gewichtet. Weitere Optionen: Potenzgesetz, Beam-Suche.           |
| Diff-Bearbeitungen / Vollständige Neufassungen / Kreuzungen | 60 % / 30 % / 10 %. Die Summe muss 100 % ergeben.        |
| Archivgröße                                         | 40                                                               |
| Parallele Auswertungen                              | 2                                                                |
| Parallele Vorschläge                                | 2                                                                |

## Verwandte Seiten

- [So funktioniert die Optimierung](/docs/concepts/optimization)
- [Metriken und Scorer](/docs/concepts/scorers)
- [Durchläufe](/docs/concepts/runs)
- [Budgets und Abrechnung](/docs/concepts/billing)
