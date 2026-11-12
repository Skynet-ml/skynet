Ein Datensatz ist eine Tabelle mit Beispielen, aus denen ein Optimierungsdurchlauf lernt und an denen er bewertet. Diese Anleitung zeigt, wie Sie eine Datei hochladen, Zeilen und Spalten bearbeiten, Spaltenrollen festlegen und einen Datensatz teilen. Die Konzepte finden Sie unter [Datensätze und Aufteilungen](/docs/concepts/datasets).

## Unterstützte Dateien

Skynet liest diese Dateitypen:

| Endung          | Wie Skynet sie liest                                                                          |
| --------------- | --------------------------------------------------------------------------------------------- |
| `.csv`          | Die erste Zeile ist die Kopfzeile. Die Datei benötigt mindestens eine Datenzeile.             |
| `.json`         | Ein Array von Objekten oder ein einzelnes Objekt. Die Schlüssel des ersten Objekts werden zu den Spalten. |
| `.xlsx`, `.xls` | Skynet liest nur das erste Tabellenblatt.                                                     |

Andere Endungen lehnt Skynet mit der Meldung "Unsupported file format" ab.

> [!TIP]
> Legen Sie die erwartete Ausgabe in die letzte Spalte. Der Einreichungsassistent markiert die letzte Spalte standardmäßig als Ausgabe.

### Beispiel-CSV

```csv
question,answer
What is the capital of France?,Paris
How many legs does a spider have?,8
```

### Beispiel-JSON

```json
[
  { "question": "What is the capital of France?", "answer": "Paris" },
  { "question": "How many legs does a spider have?", "answer": "8" }
]
```

## Einen Datensatz hochladen

1. Öffnen Sie **Datensätze**.
2. Klicken Sie auf **Datensatz hochladen** oder ziehen Sie eine Datei auf die Seite. Während Sie ziehen, zeigt die Seite „Zum Hochladen ablegen“.
3. Warten Sie auf die Meldung „Datensatz in deiner Bibliothek gespeichert“.

Skynet verwendet den Dateinamen ohne Endung als Namen des Datensatzes. Um ihn zu ändern, wählen Sie den Datensatz aus und klicken Sie auf **Umbenennen**.

> [!NOTE]
> Jedes Konto hat ein Speicherkontingent. Überschreitet ein Upload das Kontingent, öffnet Skynet einen Dialog, der das Limit erklärt. Siehe [Limits](/docs/limits).

Sie können eine Datei auch im Einreichungsassistenten hochladen („CSV-, JSON- oder Excel-Datei hochladen“) oder im [Agentenbereich](/docs/guides/agent-panel) anhängen.

## Einen Datensatz finden

Die Bibliothek listet Ihre Datensätze auf. Mit **Nach Namen suchen** filtern Sie die Liste. Datensätze, die andere Personen mit Ihnen teilen, erscheinen unter **Mit mir geteilt**.

Wenn Sie einen Datensatz auswählen, erscheinen diese Aktionen:

- **Diesen Datensatz taggen** öffnet den [Tagger](/docs/guides/tagger) mit diesem Datensatz.
- **Daten bearbeiten** öffnet den Datensatz-Editor.
- **Teilen** öffnet den Freigabedialog.
- **Umbenennen** ändert den Namen des Datensatzes.

Weitere Aktionen sind **Kopie speichern** und **Löschen**.

> [!WARNING]
> Löschen entfernt den Datensatz dauerhaft aus Ihrer Bibliothek. Sie können das nicht rückgängig machen. Durchläufe, die mit dem Datensatz erstellt wurden, bleiben gültig, aber ihr Link zurück zum Datensatz funktioniert nicht mehr.

## Einen Datensatz bearbeiten

Der Editor zeigt den Datensatz als Raster. Er zeigt 100 Zeilen pro Seite. Mit **Nächste Zeilen** und **Vorherige Zeilen** wechseln Sie zwischen den Seiten.

### Zeilen und Spalten ändern

- **Zeile hinzufügen** und **Spalte hinzufügen** fügen am Ende neue Zellen hinzu.
- **Zeile oberhalb einfügen** und **Zeile unterhalb einfügen** fügen neben der ausgewählten Zeile eine Zeile ein.
- **Zeile duplizieren**, **Zeile löschen**, **Spalte löschen** und **Spalte umbenennen** ändern die Struktur.
- **Zum Ausfüllen ziehen** kopiert einen Zellwert in die benachbarten Zellen.
- **Rückgängig** und **Wiederholen** machen Ihre letzte Änderung rückgängig oder wiederholen sie.

Doppelklicken Sie auf eine Zeile, um sie vollständig anzuzeigen.

### Speichern

Der Editor speichert Ihre Änderungen automatisch, kurz nach jeder Bearbeitung. Während des Speicherns zeigt der Status **Wird gespeichert…**. Schlägt das Speichern fehl, zeigt der Status **Speichern fehlgeschlagen**.

> [!NOTE]
> Der Editor kann keinen Datensatz ohne Zeilen speichern. Er zeigt „Fügen Sie eine Zeile hinzu, um Ihre Änderungen zu speichern“, bis Sie eine Zeile hinzufügen.

Klicken Sie auf **Zurück zur Bibliothek**, wenn Sie fertig sind.

### Nur-Lese-Zugriff

Haben Sie bei einem geteilten Datensatz die Rolle Betrachter, öffnet sich der Editor im Nur-Lese-Modus. Um die Daten zu ändern, bitten Sie den Eigentümer um die Rolle Bearbeiter oder verwenden Sie **Kopie speichern**.

### Mit KI taggen

Der Editor hat die Aktion **Mit KI taggen**. Sie öffnet den Tagger, in dem ein KI-Modell Ihnen beim Hinzufügen von Labels hilft. Siehe [Daten mit dem Tagger labeln](/docs/guides/tagger).

## Spaltenrollen festlegen

Ein Durchlauf muss wissen, welche Spalten Eingaben sind und welche Spalte die erwartete Ausgabe ist. Diese Rollen legen Sie fest, wenn Sie einen Durchlauf einreichen, nicht im Datensatz-Editor.

Im Einreichungsassistenten zeigt der Schritt „So werden die Daten gelesen“ jede Spalte mit drei Optionen:

- **Eingabe**: Ihr Scorer erhält diesen Wert in `case["input"]`.
- **Ausgabe**: die erwartete Antwort, mit der der Scorer vergleicht.
- **Überspringen**: Der Durchlauf ignoriert diese Spalte.

Die Standardwerte sind:

- Jede Spalte ist eine Eingabe, und die letzte Spalte ist die Ausgabe.
- Ein Datensatz mit einer Spalte hat eine Eingabe und keine Ausgabe.

Ohne Ausgabespalte verwendet Skynet die erste Zeile als eine Eingabe. Sie müssen mindestens eine Eingabespalte wählen.

Einige Spalten können auch Bilder enthalten. Wählen Sie für diese Spalten **Bild** oder **Text**, um Skynet mitzuteilen, wie es die Werte lesen soll.

> [!TIP]
> Der Datensatz-Editor zeigt bei Ausgabespalten ein Abzeichen „Ausgabe“. Eine neue Spalte, die Sie im Editor hinzufügen, beginnt als Eingabe.

Der Assistent teilt die Zeilen außerdem in Teile für Training und Auswertung auf. Die Teile müssen zusammen 1 ergeben. Andernfalls zeigt der Assistent „Die Aufteilung muss insgesamt 1 ergeben.“ Den vollständigen Ablauf finden Sie unter [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit).

## Einen Datensatz teilen

Wählen Sie einen Datensatz aus und klicken Sie auf **Teilen**. Der Dialog **Teilen und Datenschutz** öffnet sich.

### Link-Zugriff

| Option                  | Wer den Datensatz öffnen kann                                              |
| ----------------------- | -------------------------------------------------------------------------- |
| **Eingeschränkt**       | Nur der Eigentümer und eingeladene Personen.                               |
| **Jeder mit dem Link**  | Jede angemeldete Person mit dem Link, mit der Rolle, die Sie wählen.       |

Wählen Sie für einen Datensatz mit „Jeder mit dem Link“ die Rolle, die der Link vergibt: **Betrachter** oder **Bearbeiter**. Klicken Sie auf **Link kopieren**, um die URL zu kopieren.

### Personen einladen

1. Geben Sie unter **Personen einladen** einen Benutzernamen in **Personen per Benutzername hinzufügen** ein.
2. Klicken Sie auf **Einladen**.
3. Prüfen Sie die Liste unter **Personen mit Zugriff**. Mit **Rolle ändern** ändern Sie die Rolle einer Person.

Die Rollen sind:

| Rolle       | Zugriff                                                                  |
| ----------- | ------------------------------------------------------------------------ |
| Eigentümer  | Volle Kontrolle, einschließlich Verwaltung der Freigabe und Löschen.     |
| Bearbeiter  | Betrachterzugriff plus Ausführen des Modells, Chat und Umbenennen.       |
| Betrachter  | Öffnet den Datensatz. Der Editor ist schreibgeschützt.                   |

### Eigentum übertragen

Mit **Eigentum übertragen** geben Sie den Datensatz an eine andere Person weiter. Skynet bittet Sie um eine Bestätigung.

> [!WARNING]
> Nach der Übertragung werden Sie zum Bearbeiter und verlieren die Rechte des Eigentümers.

## Nächste Schritte

- [Starten Sie einen Durchlauf](/docs/guides/submit) mit Ihrem Datensatz.
- [Labeln Sie Daten mit dem Tagger](/docs/guides/tagger), wenn Ihre Zeilen noch keine Labels haben.
- Lesen Sie mehr über [Scorer](/docs/concepts/scorers), um festzulegen, wie Skynet Ausgaben vergleicht.
