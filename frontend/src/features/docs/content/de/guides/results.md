Die Seite des Durchlaufs zeigt den Fortschritt und die Ergebnisse eines Durchlaufs. Diese Anleitung erklärt die Tabs der Durchlaufseite, die Scores und wie Sie die beste Version exportieren.

## Tabs der Durchlaufseite

Die Seite des Durchlaufs zeigt nur die Tabs, die für den Durchlauf gelten. Einige Tabs hängen vom Status und von Ihrer Erfahrungsstufe ab.

| Tab                     | Wann er erscheint                                                  | Was er enthält                                                                  |
| ----------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| **Übersicht**           | Immer                                                              | Fortschritt, Scores und der Score-Verlauf                                       |
| **Artefakt**            | Ein Durchlauf mit Ergebnis                                         | Die Versionen Ihres Repositorys, mit der besten Version                         |
| **Daten**               | Ein abgeschlossener Durchlauf mit Fällen                           | Die Aufteilung der Daten in Trainings-, Validierungs- und Testmenge             |
| **Code**                | Wenn der Durchlauf Code zum Anzeigen hat                           | Der Scorer                                                                      |
| **Logs**                | Die meisten Durchläufe                                             | Das Log des Durchlaufs                                                          |
| **Nutzung und Kosten**  | Wenn Sie angemeldet sind                                           | Was der Durchlauf ausgegeben hat                                                |
| **Geplanter Lauf**      | Ein erfolgreicher Durchlauf                                        | Ein Zeitplan, um die Optimierung ab der besten Version erneut auszuführen       |
| **Konfiguration**       | Immer                                                              | Die Konfiguration des Durchlaufs                                                |

> [!NOTE]
> In der Stufe **Geführt** blendet die Seite des Durchlaufs die Tabs **Daten**, **Code**, **Logs** und **Konfiguration** aus. Ein direkter Link auf einen dieser Tabs öffnet ihn trotzdem. Die Stufe ändern Sie unter **Einstellungen → Konto**.

## Scores

Skynet wählt Versionen anhand der Validierungsfälle aus. Den endgültigen Score meldet es auf den Testfällen. Die Engines sehen die Testfälle nie. Lesen Sie [Datensätze und Aufteilungen](/docs/concepts/datasets).

Der Tab **Übersicht** zeigt:

- **Verbesserung**: die Änderung von der Ausgangsversion zur besten Version.
- **Score-Verlauf**: den Score über den Durchlauf hinweg.
- **Score-Aufschlüsselung**: jeden Score mit den Spalten **Baseline**, **Optimiert** und **Änderung**.

Solange der Durchlauf aktiv ist, zeigt die Übersicht auch **Verstrichen**, **Verbleibend** und **Rate**.

Die Übersicht zeigt auch **Benannte Punktzahlen**, wenn Ihr Scorer benannte Scores meldet.

### Wenn die beste Version die Ausgangsversion ist

Manchmal schlägt kein Kandidat die Ausgangsversion. Skynet gibt dann die Ausgangsversion zurück. Der Tab **Artefakt** teilt Ihnen mit, dass ein Regressionsschutz angewendet wurde.

Hat der Durchlauf an seinem Budget gestoppt, teilt Ihnen die Seite des Durchlaufs mit, welche Version Skynet gespeichert hat. Lesen Sie [Durchläufe](/docs/concepts/runs).

## Die beste Version exportieren

Der Tab **Artefakt** listet die **Versionen** Ihres Repositorys auf. Für jede Version können Sie:

- die Version kopieren.
- die Version als Datei herunterladen.
- die Version als ZIP des gesamten Repositorys herunterladen.

Sie können eine Version mit einer anderen Version vergleichen. Der Tab zeigt auch das **Feedback des Bewerters** für eine Version.

### Branches in einem neuen Repository

Hat Skynet das Repository für den Durchlauf auf GitHub erstellt, pusht es jeden Kandidaten auf einen eigenen Branch, `skynet/candidate-<version id>`. Der Branch `skynet/best` zeigt immer auf die beste Version. Er bewegt sich, wenn eine bessere Version erscheint.

## Die Ergebnisse teilen

Sie können einen Durchlauf mit anderen Personen teilen oder ihn in der öffentlichen Galerie „Erkunden“ listen. Lesen Sie [Teilen und veröffentlichen](/docs/guides/sharing).
