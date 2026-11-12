Ein Datensatz ist eine Tabelle mit Fällen. Jede Zeile ist ein Fall. Skynet bewertet jede Version Ihres Repositorys auf den Fällen mit Ihrem Scorer und vergleicht Versionen anhand der Scores. Diese Seite erklärt, wie Skynet die Fälle liest, was die Spaltenrollen bewirken und wie Skynet die Fälle in Trainings-, Validierungs- und Testmenge aufteilt.

## Daten laden

Im Schritt **Eingaben** des Assistenten haben Sie zwei Möglichkeiten:

- **CSV-, JSON- oder Excel-Datei hochladen**. Skynet akzeptiert Dateien vom Typ `.csv`, `.json`, `.xlsx` und `.xls`.
- **Aus der Bibliothek wählen**. Verwenden Sie einen Datensatz, den Sie früher gespeichert haben.

Ein Durchlauf kann höchstens 200.000 Fälle haben.

Wie Sie gespeicherte Datensätze verwalten, lesen Sie unter [Einen Datensatz vorbereiten](/docs/guides/datasets).

## Spaltenrollen

Geben Sie unter **So werden die Daten gelesen** jeder Spalte eine Rolle:

| Rolle        | Was Skynet mit der Spalte tut                                         |
| ------------ | --------------------------------------------------------------------- |
| Eingabe      | Legt den Wert in `case["input"]` ab, den Ihr Scorer erhält           |
| Ausgabe      | Legt den Wert in `case["expected"]` ab, den Ihr Scorer erhält                |
| Überspringen | Verwendet die Spalte nicht                                            |

Jede Spalte hat außerdem eine Art: **Text** oder **Bild**.

Nur eine Spalte kann die Ausgabespalte sein. Wenn Sie eine zweite Spalte auf Ausgabe setzen, wechselt die erste zu Überspringen.

### Was Ihr Code erhält

Für jede Zeile baut Skynet einen Fall:

- `input` ist ein Dict. Es hat einen Schlüssel für jede Eingabespalte.
- `expected` ist ein Dict mit einem Schlüssel: dem Namen der Ausgabespalte.

Für diese Tabelle:

| message             | label   | id  |
| ------------------- | ------- | --- |
| I was charged twice | billing | 17  |

mit den Rollen `message` = Eingabe, `label` = Ausgabe und `id` = Überspringen erhält Ihr Code:

```python tab="input"
{"message": "I was charged twice"}
```

```python tab="expected"
{"label": "billing"}
```

> [!TIP]
> Lesen Sie den erwarteten Wert in Ihrem Scorer über den Spaltennamen, zum Beispiel `case["expected"]["label"]`.

## Datenmodi

Die Rollen legen den Datenmodus des Durchlaufs fest:

| Modus        | Wann                          | Was passiert                                                                                              |
| ------------ | ----------------------------- | --------------------------------------------------------------------------------------------------------- |
| Paare        | Eine Spalte ist Ausgabe       | Jede Zeile ist ein Fall mit einer Eingabe und einem erwarteten Wert. Sie benötigen mindestens 2 Zeilen.   |
| Einzeln      | Keine Spalte ist Ausgabe      | Der Durchlauf verwendet nur die Eingabespalten der ersten Zeile. Das ist ein Fall ohne erwarteten Wert.   |

Im Einzelmodus verwendet Skynet den einen Fall für Training, Auswahl und Bericht. Nichts wird zurückgehalten.

## Die Aufteilung

Im Paarmodus teilt Skynet die Fälle in drei Mengen auf:

| Menge                 | Wofür sie dient                                                       |
| --------------------- | --------------------------------------------------------------------- |
| Training              | Die Engine schlägt auf diesen Fällen Versionen vor und testet sie.    |
| Validierung           | Skynet wählt auf diesen Fällen die beste Version aus.                 |
| Test                  | Skynet meldet auf diesen Fällen den endgültigen Score.                |

> [!NOTE]
> Die Testfälle erreichen nie eine Engine, einen Proposer oder die Vertragsprüfung. Der endgültige Score beruht auf Fällen, die der Optimierer nicht gesehen hat.

### Die Standardaufteilung

| Anzahl der Paare | Training | Validierung | Test |
| ---------------- | -------- | ----------- | ---- |
| 5 oder mehr      | 70 %     | 15 %        | 15 % |
| Weniger als 5    | 85 %     | 15 %        | 0 %  |

Mit weniger als 5 Paaren behält Skynet keine Testmenge zurück.

### Wie Skynet die Fälle zählt

Skynet rechnet die Anteile mit dem Verfahren der größten Reste in ganze Fallzahlen um. Die Anzahlen ergeben immer die Gesamtzahl der Fälle. Eine Menge mit einem Anteil über null erhält mindestens einen Fall. Eine Menge mit dem Anteil null erhält keine Fälle.

Vor der Aufteilung mischt Skynet die Fälle. Das Mischen verwendet den Seed des Durchlaufs. Wenn Sie keinen Seed angeben, erzeugt Skynet einen aus den Fällen, dem Ziel und dem Artefakt. Reichen Sie denselben Durchlauf erneut ein, erhalten Sie also dieselbe Aufteilung.

### Ausweichregeln

- Mit weniger als 5 Paaren entfernt Skynet die Testmenge und skaliert Training und Validierung so, dass sie zusammen 1 ergeben.
- Ist die Trainingsmenge leer, verwendet Skynet die Validierungsfälle als Training.
- Gibt es keine Validierungsmenge, wählt Skynet auf den Trainingsfällen aus.
- Gibt es keine Testmenge, meldet Skynet auf der Validierungsmenge. Gibt es auch keine Validierungsmenge, meldet es auf der Trainingsmenge.

> [!WARNING]
> Ein Score, den Skynet auf Training oder Validierung meldet, ist kein Score auf zurückgehaltenen Daten. Verwenden Sie mindestens 5 Paare, um eine Testmenge zu erhalten.

## Die Aufteilung ändern

Der Abschnitt zur Aufteilung zeigt **Empfohlene Aufteilung**. Er hat zwei Modi:

1. **Empfehlung verwenden**: Skynet verwendet die oben beschriebene Standardaufteilung.
2. **Manuelle Auswahl**: Sie legen **Training**, **Validierung** und **Test** selbst fest.

Bei der manuellen Auswahl nimmt jedes Feld einen Anteil von 0 bis 1 in Schritten von 0,05 an. Jedes Feld zeigt die Anzahl der Fälle, die es erhält. Die drei Anteile müssen zusammen 1 ergeben. Andernfalls zeigt der Assistent „Die Aufteilung muss insgesamt 1 ergeben.“

## Beispielfälle für die Vertragsprüfung

Die Vertragsprüfung im Schritt **Metrik & Ausführung** führt Ihr Setup und Ihren Scorer auf der Startversion aus, auf 3 Beispielfällen. Skynet nimmt diese Beispiele nur aus den Trainings- und Validierungsfällen, nie aus dem Test.

## Verwandte Seiten

- [Einen Datensatz vorbereiten](/docs/guides/datasets)
- [Metriken und Scorer](/docs/concepts/scorers)
- [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit)
- [Limits](/docs/limits)
