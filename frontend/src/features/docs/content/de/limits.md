Skynet setzt Limits für Durchläufe, Dateien, Speicher und Anfrageraten. Die Limits halten die Plattform stabil und schützen Ihr Guthaben. Diese Seite listet die Standardwerte auf. Ein Administrator kann einige Limits für ein Konto ändern.

> [!NOTE]
> Die Werte auf dieser Seite sind die aktuellen Standardwerte. Wenn Sie ein Limit erreichen, teilt Ihnen die App oder die API mit, welches Limit die Anfrage gestoppt hat.

## Durchläufe

| Limit                              | Free | Pro   |
| ---------------------------------- | ---- | ----- |
| Gleichzeitig aktive Durchläufe     | 5    | 20    |
| Gespeicherte Durchläufe            | 100  | 1.000 |

Ein aktiver Durchlauf ist ein Durchlauf mit dem Status `pending`, `validating`, `running` oder `paused`. Haben Sie die Höchstzahl aktiver Durchläufe erreicht, lehnt Skynet neue Durchläufe ab, bis einer davon endet.

Das Limit für gespeicherte Durchläufe zählt alle Ihre Durchläufe. Löschen Sie Durchläufe, die Sie nicht benötigen, um Platz für neue zu schaffen.

Schlägt eine Pro-Verlängerung fehl, behält das Konto die Pro-Limits 7 Tage lang. Danach gelten die Free-Limits, bis die Zahlung erfolgreich ist.

| Limit                                              | Wert                                                                  |
| -------------------------------------------------- | --------------------------------------------------------------------- |
| Kopien in einer API-Klonanfrage                    | 1 bis 5                                                               |
| Automatische Wiederherstellungsversuche pro Durchlauf | 3                                                                  |
| Beispielfälle in der Vertragsprüfung               | 3                                                                     |
| Gespeicherte Fortschrittsereignisse pro Durchlauf  | 5.000. Skynet entfernt zuerst die ältesten Ereignisse.                |
| Lebensdauer einer Sandbox                          | 45 Minuten. Ein Durchlauf, der mehr Zeit braucht, öffnet eine neue Sandbox. |

Wie Wiederherstellungsversuche funktionieren, lesen Sie unter [Durchläufe](/docs/concepts/runs).

## Speicher

| Limit           | Free    | Pro    |
| --------------- | ------- | ------ |
| Gesamtspeicher  | 500 MiB | 20 GiB |

Der Gesamtspeicher umfasst Ihre Durchläufe, Datensätze, Logs, Agenten-Chats, bereitgestellte Uploads und Suchdaten. Skynet lehnt einen Schreibvorgang ab, der Ihr Speicherlimit überschreiten würde.

## Dateien und Datensätze

| Limit                                                 | Wert                                      |
| ----------------------------------------------------- | ----------------------------------------- |
| Datensatzdatei, die in Ihrer Bibliothek gespeichert wird | 50 MiB pro Datei, nach der Komprimierung |
| Zeilen aus einem Connector, der Datensätze importiert | 20.000                                    |
| An einen Agenten-Chat angehängte Datei                | 20 MiB                                    |
| Aus einem Chat-Anhang gelesener Text                  | 50.000 Zeichen                            |
| Audioclip für das Diktieren                           | 25 MB                                     |
| GitHub-Repository-Archiv                              | 200 MiB, komprimiert                      |
| Auf der Durchlaufseite geöffnete Repository-Datei     | 1 MiB                                     |

Zu den Connectors, die Datensätze importieren, gehören Datenbanktabellen, LLM-Traces und Notion-Datenbanken. Lesen Sie [Einen Datensatz vorbereiten](/docs/guides/datasets).

### Workspace-Dateien im Assistenten

| Limit                             | Wert                                                                    |
| --------------------------------- | ----------------------------------------------------------------------- |
| Zusätzliche Dateien im Workspace  | 20                                                                      |
| Länge eines Dateinamens           | 120 Zeichen                                                             |
| Ordnertiefe                       | 3 Ordner                                                                |
| Dateitypen                        | `.py`, `.txt`, `.md`, `.json`, `.yaml`, `.csv`, `.toml`, `.ini`, `.cfg` |

`scorer.py` enthält Ihren Scorer. Skynet ruft dessen Funktion `score(repo_path, case)` für jeden Fall auf.

## Anfrageraten

| Limit                                                                    | Wert                                               |
| ------------------------------------------------------------------------ | -------------------------------------------------- |
| Einreichungen von Durchläufen                                            | 30 pro Konto in einer gleitenden Minute            |
| Diktierclips                                                             | 120 pro Konto pro Stunde                           |
| Für Agenten-Chats gelesene Dateien                                       | 300 pro Konto pro Stunde                           |
| Kontoanfragen (Registrierung, Passwort zurücksetzen, E-Mail-Bestätigung) | 20 pro E-Mail-Adresse in einer gleitenden Stunde   |
| Runden des KI-Assistenten im Einreichungsassistenten                     | 50 pro Konto pro UTC-Tag                           |
| Tokens für Suche und Indexierung                                         | 2.000.000 pro Konto pro Monat                      |

Wenn Sie ein Ratenlimit überschreiten, gibt die API den HTTP-Status `429` zurück. Warten Sie und senden Sie die Anfrage dann erneut.

Überschreitet ein Konto seine monatlichen Tokens für Suche und Indexierung, indexiert Skynet für dieses Konto keine neuen Elemente mehr. Die Suche funktioniert dann so, als wäre die semantische Suche nicht verfügbar.

## Geld

| Limit                                                  | Wert                                                                                  |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| Standard-Ausgabenlimit für einen neuen Durchlauf       | 5 $ oder der zum Start nötige Betrag, wenn dieser höher ist                           |
| Puffer im zum Start nötigen Betrag                     | 1 $ oder die Kosten des teuersten einzelnen Modellaufrufs, wenn diese höher sind      |
| Individuelle Aufladung                                 | 5 $ bis 1.000 $                                                                       |
| Guthaben, das zum Start einer interaktiven Modellrunde nötig ist | 0,05 $                                                                      |

Gebühren und Reservierungen erklärt [Budgets und Abrechnung](/docs/concepts/billing).

## Verwandte Seiten

- [Durchläufe](/docs/concepts/runs)
- [Budgets und Abrechnung](/docs/concepts/billing)
- [FAQ](/docs/faq)
