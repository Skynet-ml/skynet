Diese Seite zeigt, wie Sie Ihren ersten Durchlauf in etwa zehn Minuten starten. Sie melden sich an, laden einen kleinen Datensatz hoch, füllen die vier Schritte des Assistenten aus und starten den Durchlauf. Im Beispiel erstellt Skynet ein neues Repository, das Support-Nachrichten Labels zuordnet, und verbessert es dann.

## Bevor Sie beginnen

Sie benötigen:

- Ein Skynet-Konto.
- Ein GitHub-Konto. Skynet erstellt das Repository in diesem Konto.
- Eine CSV-, JSON- oder Excel-Datei (`.csv`, `.json`, `.xlsx` oder `.xls`). Jede Zeile ist ein Fall.
- Eine oder mehrere Eingabespalten, zum Beispiel `message`.
- Eine Spalte mit der erwarteten Ausgabe, zum Beispiel `label`.

> [!TIP]
> Verwenden Sie mindestens 5 Zeilen. Mit weniger als 5 Paaren behält Skynet keine Testmenge zurück. Der endgültige Score stammt dann aus Fällen, die der Optimierer verwendet hat.

## 1. Anmelden

Melden Sie sich bei Skynet an. Verbinden Sie GitHub unter **Einstellungen → Connectors**. Öffnen Sie dann die Einreichungsseite unter `/submit`.

> [!NOTE]
> Diese Seite verwendet die Bezeichnungen der Stufe **Standard**. Die Stufe **Geführt** verwendet einfachere Bezeichnungen, überspringt den Schritt „Optimierer“ und wählt **Auto** für Sie. Die Stufe ändern Sie in den Einstellungen.

## 2. Aufgabe

Der erste Schritt ist **Aufgabe**. Er fragt: „Was möchten Sie optimieren?“

1. Beschreiben Sie unter **Ziel**, was ein gutes Ergebnis ist. Verwenden Sie einfache Worte, zum Beispiel „Ordne jede Support-Nachricht billing, bug oder other zu.“
2. Optional: Fügen Sie unter **Hintergrund** Kontext hinzu, der dem Optimierer hilft.
3. Wählen Sie **Neues Repository erstellen**. Geben Sie einen Namen ein, zum Beispiel `support-labels`, und wählen Sie **Privat** oder **Öffentlich**.

Skynet erstellt das Repository in Ihrem GitHub-Konto. Der Assistent schreibt aus Ihrer Aufgabe und Ihren Daten eine erste Lösung. Diese Lösung ist die Ausgangsversion. Haben Sie bereits Code, wählen Sie stattdessen **Repository verwenden**. Lesen Sie [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit).

## 3. Eingaben

Der zweite Schritt ist **Eingaben**.

1. Wählen Sie **CSV-, JSON- oder Excel-Datei hochladen** oder **Aus der Bibliothek wählen**, um einen gespeicherten Datensatz zu verwenden.
2. Geben Sie unter **So werden die Daten gelesen** jeder Spalte eine Rolle:
   - **Eingabe**: Ihr Code erhält diese Spalte.
   - **Ausgabe**: der erwartete Wert. Der Scorer erhält ihn.
   - **Überspringen**: Skynet verwendet diese Spalte nicht.
3. Behalten Sie für die Aufteilung **Empfehlung verwenden** bei. Für die meisten Datensätze beträgt die Aufteilung 70 % Training, 15 % Validierung und 15 % Test.

Die vollständigen Regeln stehen unter [Datensätze und Aufteilungen](/docs/concepts/datasets).

## 4. Optimierer

Der dritte Schritt ist **Optimierer** („Optimierer, Modelle und Budget“).

- Belassen Sie den Optimierer auf **Auto**.
- Wählen Sie ein **Reflexionsmodell**. Der Optimierer schreibt damit neue Versionen. Dieses Modell ist erforderlich.
- Belassen Sie unter **Budget** den Wert **Max. Bewertungsläufe** auf dem Standardwert 200.

Unter [Engines](/docs/concepts/engines) erfahren Sie, wie Sie eine bestimmte Engine wählen.

## 5. Metrik & Ausführung

Der letzte Schritt ist **Metrik & Ausführung**. Der Assistent schreibt den Scorer für Sie. Sie können ihn bearbeiten. Der Scorer erhält den Pfad einer Version des Repositorys und einen Fall:

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = str(module.serve(case["input"])).strip().lower()
    want = case["expected"]["label"].strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`case["input"]` enthält die Eingabespalten eines Falls. `case["expected"]` enthält die Ausgabespalte, mit ihrem Spaltennamen als Schlüssel. Das Repository hat eine Datei `serve.py` mit `def serve(input)`. Skynet stellt mit derselben Funktion nach dem Durchlauf die beste Version bereit.

Führen Sie dann die Prüfung aus. Die Prüfung führt Ihr Setup und Ihren Scorer in der Sandbox auf der Ausgangsversion aus. Sie können den Durchlauf erst starten, wenn die Prüfung bestanden ist.

## 6. Starten und beobachten

Der Übersichtsbildschirm zeigt „Bereit zum Start“. Seine Tabs sind Allgemein, Daten, Modelle, Budget und Belohnung. Prüfen Sie die Einrichtung und starten Sie dann die Optimierung.

Öffnen Sie den Durchlauf aus Ihrer Liste der Optimierungen, um ihn zu beobachten. Skynet pusht jeden Kandidaten auf einen eigenen Branch in Ihrem neuen Repository. Der Branch `skynet/best` zeigt immer auf die beste Version. Unter [Ergebnisse lesen](/docs/guides/results) erfahren Sie, wie Sie die Scores und die Gewinnerversion verstehen.

## Nächste Schritte

- Schreiben Sie einen besseren Scorer: [Metriken und Scorer](/docs/concepts/scorers).
- Erfahren Sie, wie die Engines arbeiten: [So funktioniert die Optimierung](/docs/concepts/optimization).
- Erfahren Sie, was ein Durchlauf kostet: [Budgets und Abrechnung](/docs/concepts/billing).
