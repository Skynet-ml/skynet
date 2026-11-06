Diese Seite zeigt, wie Sie Ihren ersten Durchlauf in etwa zehn Minuten starten. Sie melden sich an, laden einen kleinen Datensatz hoch, füllen die vier Schritte des Assistenten aus und starten den Durchlauf. Das Beispiel verbessert ein DSPy-Programm, das Support-Nachrichten Labels zuordnet.

## Bevor Sie beginnen

Sie benötigen:

- Ein Skynet-Konto.
- Eine CSV-, JSON- oder Excel-Datei (`.csv`, `.json`, `.xlsx` oder `.xls`). Jede Zeile ist ein Fall.
- Eine oder mehrere Eingabespalten, zum Beispiel `message`.
- Eine Spalte mit der erwarteten Ausgabe, zum Beispiel `label`.

> [!TIP]
> Verwenden Sie mindestens 5 Zeilen. Mit weniger als 5 Paaren behält Skynet keine Testmenge zurück. Der endgültige Score stammt dann aus Fällen, die der Optimierer verwendet hat.

## 1. Anmelden

Melden Sie sich bei Skynet an. Öffnen Sie dann die Einreichungsseite unter `/submit`.

> [!NOTE]
> Diese Seite verwendet die Bezeichnungen der Stufe **Standard**. Die Stufe **Geführt** verwendet einfachere Bezeichnungen, überspringt den Schritt „Optimierer“ und wählt **Auto** für Sie. Die Stufe ändern Sie in den Einstellungen.

## 2. Aufgabe

Der erste Schritt ist **Aufgabe**. Er fragt: „Was möchten Sie optimieren?“

1. Beschreiben Sie unter **Ziel**, was ein gutes Ergebnis ist. Verwenden Sie einfache Worte, zum Beispiel „Ordne jede Support-Nachricht billing, bug oder other zu.“
2. Optional: Fügen Sie unter **Hintergrund** Kontext hinzu, der dem Optimierer hilft.
3. Wählen Sie unter **Was optimiert wird** die Option **DSPy-Programm**.
4. Fügen Sie unter **Ausgangsversion** die aktuelle Anweisung Ihres Programms ein.

## 3. Eingaben

Der zweite Schritt ist **Eingaben**.

1. Wählen Sie **CSV-, JSON- oder Excel-Datei hochladen** oder **Aus der Bibliothek wählen**, um einen gespeicherten Datensatz zu verwenden.
2. Geben Sie unter **So werden die Daten gelesen** jeder Spalte eine Rolle:
   - **Eingabe**: Der Ausführungscode erhält diese Spalte.
   - **Ausgabe**: der erwartete Wert. Die Metrik erhält ihn.
   - **Überspringen**: Skynet verwendet diese Spalte nicht.
3. Behalten Sie für die Aufteilung **Empfehlung verwenden** bei. Für die meisten Datensätze beträgt die Aufteilung 70 % Training, 15 % Validierung und 15 % Test.

Die vollständigen Regeln stehen unter [Datensätze und Aufteilungen](/docs/concepts/datasets).

## 4. Optimierer

Der dritte Schritt ist **Optimierer** („Optimierer, Modelle und Budget“).

- Belassen Sie den Optimierer auf **Auto**.
- Wählen Sie ein **Reflexionsmodell**. Der Optimierer schreibt damit neue Versionen. Dieses Modell ist erforderlich.
- Wählen Sie ein **Aufgabenmodell**. Ihr Programm verwendet es. Ein DSPy-Programm braucht eines.
- Belassen Sie unter **Budget** den Wert **Max. Bewertungsläufe** auf dem Standardwert 200.

Unter [Engines](/docs/concepts/engines) erfahren Sie, wie Sie eine bestimmte Engine wählen.

## 5. Metrik & Ausführung

Der letzte Schritt ist **Metrik & Ausführung** („Code und Metrik“). Sie schreiben zwei Dateien.

```python tab="run.py"
import dspy

program = dspy.Predict("message -> label")


def run(artifact, input):
    return program(message=input["message"]).label
```

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"].strip().lower()
    got = str(output).strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`artifact` ist die aktuelle Anweisung, die der Optimierer vorschlägt. Skynet wendet sie auf `program` an. `input` enthält die Eingabespalten eines Falls. `expected` enthält die Ausgabespalte, mit ihrem Spaltennamen als Schlüssel.

Wählen Sie dann unter **Vertragsprüfung** die Schaltfläche **Prüfen**. Die Prüfung führt Ihren Code und Ihre Metrik in der Sandbox auf 3 Beispielfällen aus. Sie prüft:

| Prüfung                            | Was fehlschlägt                                                   |
| ---------------------------------- | ----------------------------------------------------------------- |
| Signatur des Ausführungscodes      | `run.py` definiert nicht `run(artifact, input)`                   |
| Signatur der Metrik                | `metric.py` definiert nicht `score(input, output, expected=None)` |
| Metrikmodell                       | Das Metrikmodell ist nicht nutzbar                                |
| Ausführungscode auf Beispielen     | Ihr Ausführungscode löst bei einem Beispielfall einen Fehler aus  |
| Rückgabewert der Metrik            | `score` gibt keinen gültigen Score mit Feedback zurück            |

> [!NOTE]
> Sie können den Durchlauf erst starten, wenn die Vertragsprüfung bestanden ist.

## 6. Starten und beobachten

Der Übersichtsbildschirm zeigt „Bereit zum Start“. Seine Tabs sind Allgemein, Daten, Modelle, Budget und Belohnung. Prüfen Sie die Einrichtung und starten Sie dann die Optimierung.

Öffnen Sie den Durchlauf aus Ihrer Liste der Optimierungen, um ihn zu beobachten. Unter [Ergebnisse lesen](/docs/guides/results) erfahren Sie, wie Sie die Scores und die Gewinnerversion verstehen.

## Nächste Schritte

- Schreiben Sie eine bessere Metrik: [Metriken und Scorer](/docs/concepts/scorers).
- Erfahren Sie, welche Engines zu Ihrem Artefakt passen: [So funktioniert die Optimierung](/docs/concepts/optimization).
- Erfahren Sie, was ein Durchlauf kostet: [Budgets und Abrechnung](/docs/concepts/billing).
