Der Einreichungsassistent richtet einen Durchlauf ein und startet ihn. Er hat vier Schritte: **Aufgabe**, **Eingaben**, **Optimierer** und **Metrik & Ausführung**. Jeder Schritt hat einen oder mehrere Bildschirme. Diese Anleitung erklärt jeden Bildschirm, von der Aufgabe bis zur Vertragsprüfung und zum Start des Durchlaufs.

> [!NOTE]
> Der Assistent zeigt je nach Erfahrungsstufe mehr oder weniger Einstellungen: **Geführt**, **Standard** oder **Experte**. Geführt ist der Standard. Die Stufe ändern Sie unter **Einstellungen → Konto** in **Wie viel angezeigt wird**. Einige Bezeichnungen sind in der Stufe Geführt einfacher. Diese Anleitung verwendet die Bezeichnungen der Stufen Standard und Experte und nennt die Bezeichnung der Stufe Geführt in Klammern, wo sie abweicht.

## Schritt 1: Aufgabe

### Was möchten Sie optimieren?

Beschreiben Sie dem Assistenten Ihre Aufgabe im Chat **Aufgabeninterview**. Der Assistent füllt zwei Felder für Sie aus:

- **Ziel** (Geführt: **Ziel**): was das optimierte Artefakt gut können muss.
- **Hintergrund** (Geführt: **Zusätzlicher Kontext**): Kontext, Einschränkungen und wie ein gutes Ergebnis aussieht.

Sie können beide Felder bearbeiten. Der Assistent überschreibt Ihre Änderungen nicht.

### Was optimiert wird

Wählen Sie unter **Was optimiert wird** die Option **DSPy-Programm** oder **Ein GitHub-Repository**.

Geben Sie dann die **Ausgangsversion** an (Geführt: **Unser Ausgangspunkt**). Fügen Sie die aktuelle Version ein oder lassen Sie den Assistenten einen Entwurf schreiben.

### Ein Repository optimieren

Sie können auch Code in einem Repository optimieren. Legen Sie diese Felder fest:

- **Repository**: Wählen Sie ein Repository oder schreiben Sie es als `owner/name`.
- **Commit**: ein SHA mit 40 Zeichen. Lassen Sie das Feld leer, um den neuesten Commit des Branches zu verwenden.
- **Pfade, die der Optimierer ändern darf**: eine Datei oder ein Ordner pro Zeile. Ein Punkt steht für das gesamte Repository.
- **Setup-Befehl**: läuft in jedem frischen Checkout vor der Bewertung. Lassen Sie das Feld leer, dann ermittelt Skynet den Befehl.

## Schritt 2: Eingaben

### Daten

Fügen Sie die Fälle für den Durchlauf hinzu. Sie können:

- eine CSV-, JSON- oder Excel-Datei hochladen.
- einen Datensatz aus Ihrer Bibliothek wählen.

Lesen Sie [Datensätze und Aufteilungen](/docs/concepts/datasets) und [Einen Datensatz vorbereiten](/docs/guides/datasets).

### So werden die Daten gelesen

Der Assistent liest die Daten und schlägt vor, wie sie verwendet werden. Prüfen Sie seine Auswahl:

- Markieren Sie jede Spalte als Eingabe, als erwartete Ausgabe oder ignorieren Sie sie.
- Ohne Ausgabespalte verwendet Skynet die erste Zeile als eine Eingabe.
- Legen Sie die Aufteilung in Trainings-, Validierungs- und Testmenge fest. Die Aufteilung muss insgesamt 1 ergeben.

> [!WARNING]
> Sie können die späteren Schritte nicht öffnen, solange der Schritt **Eingaben** ein Problem hat. Beheben Sie die Probleme, die der Assistent anzeigt, und fahren Sie dann fort.

## Schritt 3: Optimierer

In der Stufe Standard hat dieser Schritt einen Bildschirm. Er zeigt den Optimierer, das Budget und bei Bedarf die Modelle. In den Stufen Geführt und Experte hat der Schritt drei Bildschirme: **Optimierer**, **Modelle** und **Budget**.

### Optimierer

Der Assistent listet nur die Optimierer auf, die zu Ihrer Aufgabe und Ihren Eingaben passen. **Auto** ist für Sie ausgewählt. Wählen Sie bei Bedarf einen anderen Optimierer. Lesen Sie [Engines](/docs/concepts/engines) und [So funktioniert die Optimierung](/docs/concepts/optimization).

### Modelle

- **Aufgabenmodell**: das Modell, das Ihr Ausführungscode aufruft. Lassen Sie es leer, wenn Ihr Ausführungscode kein Modell aufruft. Ein DSPy-Programm muss ein Aufgabenmodell haben.
- **Reflexionsmodell**: das Modell, das Scores und Feedback liest und die nächsten Kandidaten schreibt. Bei einem Coding-Agent-Optimierer ist dies das Modell des Agenten.

### Budget

Legen Sie fest, wie lange der Durchlauf weiter versuchen darf und wie viel er ausgeben darf:

- **Max. Bewertungsläufe** (Geführt: **Max. Versuche**): die höchste Anzahl an Kandidaten, die Skynet bewertet. Einige Optimierer benötigen eine Mindestanzahl.
- **Stoppen bei Bewertung** (Geführt: **Früh stoppen, sobald die Bewertung erreicht**): Der Durchlauf stoppt, wenn eine Version diesen Score erreicht.
- **Max. Iterationen** (Geführt: **Max. Runden**): wird für Optimierer angezeigt, die das unterstützen.
- **Ausgabenlimit**: der Höchstbetrag, den der Durchlauf ausgeben darf. Der Standard ist 5 $ oder der Betrag **Zum Start nötig**, wenn dieser höher ist. Sie können auch **Kein Limit** wählen.

Der Assistent lässt Sie nicht weitermachen, wenn das Limit oder Ihr Guthaben unter dem zum Start nötigen Betrag liegt. Lesen Sie [Budgets und Abrechnung](/docs/concepts/billing).

## Schritt 4: Metrik & Ausführung

### Code und Metrik

Schreiben Sie zwei Dateien:

- `run.py`: der Ausführungscode, der Ihr Artefakt auf einem Fall ausführt.
- `metric.py`: die Metrik, die die Ausgabe bewertet und Feedback schreibt.

Der **Coding-Agent** kann jede Datei für Sie schreiben und ändern. Er prüft seinen Code und behebt, was fehlschlägt. Sie können bis zu 20 zusätzliche Dateien hinzufügen. Lesen Sie [Metriken und Scorer](/docs/concepts/scorers).

Die Metrik legt ihr Modell in ihrem eigenen `llm()`-Aufruf fest. Ohne `model=` verwendet die Metrik das Reflexionsmodell.

### Vertragsprüfung

Die **Vertragsprüfung** (Geführt: **Testlauf**) führt Ihren Code und Ihre Metrik in der Sandbox auf 3 Beispielfällen aus. Sie prüft:

- die Signatur des Ausführungscodes,
- die Signatur der Metrik,
- das Metrikmodell,
- den Ausführungscode auf den Beispielen,
- den Rückgabewert der Metrik.

Wählen Sie **Prüfen** (Geführt: **Testen**). Sie können den Durchlauf erst starten, wenn die Prüfung bestanden ist. Ändern Sie nach einer bestandenen Prüfung den Code oder die Einrichtung, führen Sie die Prüfung erneut aus.

> [!TIP]
> Öffnen Sie jedes Beispiel, um Eingabe, Ausgabe und Feedback zu sehen. Gutes Feedback gibt dem Optimierer mehr Anhaltspunkte als der Score allein.

### Geplanter Lauf

Sie können die Optimierung nach einem erfolgreichen Abschluss nach Zeitplan erneut ausführen. Der Standard ist **Aus**.

### Bereit zum Start

Der letzte Bildschirm zeigt eine Übersicht in fünf Tabs: **Allgemein**, **Daten**, **Modelle**, **Budget** und **Belohnung**. Prüfen Sie die Einrichtung und starten Sie dann die Optimierung.

Die Übersicht zeigt auch die Sichtbarkeit des Durchlaufs. Neue Durchläufe sind standardmäßig öffentlich. Sie ändern das später im Dialog **Teilen und Datenschutz** auf der Seite des Durchlaufs. Lesen Sie [Teilen und veröffentlichen](/docs/guides/sharing).

> [!NOTE]
> Ein Repository-Durchlauf hat in diesem Schritt nur den Bildschirm **Code und Metrik** und die Übersicht. Skynet prüft die Einrichtung gegen das Repository, wenn Sie den Durchlauf starten.

## Nach dem Start

Die Seite des Durchlaufs öffnet sich. Lesen Sie [Durchläufe](/docs/concepts/runs) und [Ergebnisse lesen](/docs/guides/results).
