Der Einreichungsassistent richtet einen Durchlauf ein und startet ihn. Er hat vier Schritte: **Aufgabe**, **Eingaben**, **Optimierer** und **Metrik & Ausführung**. Jeder Schritt hat einen oder mehrere Bildschirme. Diese Anleitung erklärt jeden Bildschirm, von der Aufgabe bis zur Prüfung und zum Start des Durchlaufs.

> [!NOTE]
> Der Assistent zeigt je nach Erfahrungsstufe mehr oder weniger Einstellungen: **Geführt**, **Standard** oder **Experte**. Geführt ist der Standard. Die Stufe ändern Sie unter **Einstellungen → Konto** in **Wie viel angezeigt wird**. Einige Bezeichnungen sind in der Stufe Geführt einfacher. Diese Anleitung verwendet die Bezeichnungen der Stufen Standard und Experte und nennt die Bezeichnung der Stufe Geführt in Klammern, wo sie abweicht.

## Schritt 1: Aufgabe

### Was möchten Sie optimieren?

Beschreiben Sie dem Assistenten Ihre Aufgabe im Chat **Aufgabeninterview**. Der Assistent füllt zwei Felder für Sie aus:

- **Ziel** (Geführt: **Ziel**): was der optimierte Code gut können muss.
- **Hintergrund** (Geführt: **Zusätzlicher Kontext**): Kontext, Einschränkungen und wie ein gutes Ergebnis aussieht.

Sie können beide Felder bearbeiten. Der Assistent überschreibt Ihre Änderungen nicht.

### Woher der Code kommt

Skynet ist Optimize Anything: Jeder Durchlauf optimiert ein Repository. Wählen Sie eine von zwei Möglichkeiten:

- **Repository verwenden**: Code in einem GitHub-Repository optimieren, das Sie verbunden haben.
- **Neues Repository erstellen**: Skynet erstellt ein neues GitHub-Repository in Ihrem Konto. Der Assistent schreibt aus Ihrer Aufgabe und Ihren Daten eine erste Lösung. Diese Lösung ist die Ausgangsversion.

Verbinden Sie GitHub unter **Einstellungen → Connectors**, bevor Sie beginnen.

### Repository verwenden

Legen Sie diese Felder fest:

- **Repository**: Wählen Sie ein Repository oder schreiben Sie es als `owner/name`.
- **Commit**: ein SHA mit 40 Zeichen. Lassen Sie das Feld leer, um den neuesten Commit des Branches zu verwenden.
- **Pfade, die der Optimierer ändern darf**: eine Datei oder ein Ordner pro Zeile. Ein Punkt steht für das gesamte Repository.
- **Setup-Befehl**: läuft in jedem frischen Checkout vor der Bewertung. Lassen Sie das Feld leer, dann ermittelt Skynet den Befehl.

### Neues Repository erstellen

Legen Sie diese Felder fest:

- **Name**: der Name des neuen Repositorys. Ihr GitHub-Konto darf noch kein Repository mit diesem Namen haben.
- **Privat** oder **Öffentlich**: wer das Repository auf GitHub sehen kann.

Skynet pusht jeden Kandidaten auf einen eigenen Branch. Der Branch `skynet/best` zeigt auf die beste Version.

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

- **Reflexionsmodell**: das Modell, das Scores und Feedback liest und die nächsten Kandidaten schreibt. Bei einem Coding-Agent-Optimierer ist dies das Modell des Agenten.
- **Aufgabenmodell**: das Modell, das Ihr Repository-Code und Ihr Scorer über das Modell-Gateway von Skynet aufrufen können. Lesen Sie [So funktioniert die Optimierung](/docs/concepts/optimization).

### Budget

Legen Sie fest, wie lange der Durchlauf weiter versuchen darf und wie viel er ausgeben darf:

- **Max. Bewertungsläufe** (Geführt: **Max. Versuche**): die höchste Anzahl an Kandidaten, die Skynet bewertet. Einige Optimierer benötigen eine Mindestanzahl.
- **Stoppen bei Bewertung** (Geführt: **Früh stoppen, sobald die Bewertung erreicht**): Der Durchlauf stoppt, wenn eine Version diesen Score erreicht.
- **Max. Iterationen** (Geführt: **Max. Runden**): wird für Optimierer angezeigt, die das unterstützen.
- **Ausgabenlimit**: der Höchstbetrag, den der Durchlauf ausgeben darf. Der Standard ist 5 $ oder der Betrag **Zum Start nötig**, wenn dieser höher ist. Sie können auch **Kein Limit** wählen.

Der Assistent lässt Sie nicht weitermachen, wenn das Limit oder Ihr Guthaben unter dem zum Start nötigen Betrag liegt. Lesen Sie [Budgets und Abrechnung](/docs/concepts/billing).

## Schritt 4: Metrik & Ausführung

### Scorer

Schreiben Sie `scorer.py`. Die Datei definiert `score(repo_path, case)` und gibt einen Score mit Feedback zurück. Der Assistent kann sie für Sie schreiben. Lesen Sie [Metriken und Scorer](/docs/concepts/scorers).

Das Repository kann eine Datei `serve.py` mit `def serve(input)` haben. Skynet stellt damit nach dem Durchlauf die beste Version bereit. Lesen Sie [Ergebnisse lesen](/docs/guides/results).

### Secrets

Fügen Sie Secrets hinzu, zum Beispiel API-Schlüssel, die Ihr Code braucht. Jedes Secret erreicht Ihren Code und Ihren Scorer als Umgebungsvariable mit demselben Namen. Der Coding-Agent kann sie nicht sehen. Skynet verbirgt ihre Werte in den Logs.

### Prüfung

Die Prüfung (Geführt: **Testlauf**) führt das Setup und Ihren Scorer auf der Ausgangsversion in der Sandbox aus. Sie prüft:

- die Signatur des Scorers,
- den Setup-Befehl,
- den Rückgabewert des Scorers auf den Beispielfällen.

Wählen Sie **Prüfen** (Geführt: **Testen**). Sie können den Durchlauf erst starten, wenn die Prüfung bestanden ist. Ändern Sie nach einer bestandenen Prüfung den Scorer oder das Setup, führen Sie die Prüfung erneut aus.

> [!TIP]
> Öffnen Sie jedes Beispiel, um seinen Score und sein Feedback zu sehen. Gutes Feedback gibt dem Optimierer mehr Anhaltspunkte als der Score allein.

### Geplanter Lauf

Sie können die Optimierung nach einem erfolgreichen Abschluss nach Zeitplan erneut ausführen. Jeder geplante Zyklus startet ab der aktuell besten Version des Repositorys. Der Standard ist **Aus**.

### Bereit zum Start

Der letzte Bildschirm zeigt eine Übersicht in fünf Tabs: **Allgemein**, **Daten**, **Modelle**, **Budget** und **Belohnung**. Prüfen Sie die Einrichtung und starten Sie dann die Optimierung.

Die Übersicht zeigt auch die Sichtbarkeit des Durchlaufs. Neue Durchläufe sind standardmäßig öffentlich. Sie ändern das später im Dialog **Teilen und Datenschutz** auf der Seite des Durchlaufs. Lesen Sie [Teilen und veröffentlichen](/docs/guides/sharing).

## Nach dem Start

Die Seite des Durchlaufs öffnet sich. Lesen Sie [Durchläufe](/docs/concepts/runs) und [Ergebnisse lesen](/docs/guides/results).
