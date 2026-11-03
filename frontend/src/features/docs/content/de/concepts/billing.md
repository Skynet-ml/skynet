Skynet arbeitet mit einem vorausbezahlten Guthaben. Sie laden Geld auf das Guthaben. Jeder Durchlauf hat ein Ausgabenlimit, und Skynet belastet Ihr Guthaben mit der tatsächlichen Nutzung des Durchlaufs. Diese Seite erklärt das Guthaben, die Durchlaufbudgets, Reservierungen, Gebühren und Ihre eigenen Anbieterschlüssel.

> [!NOTE]
> Die Zahlen auf dieser Seite sind die aktuellen Standardwerte. Die App zeigt die aktuellen Werte. Öffnen Sie **Einstellungen → Abrechnung**, um Ihr Guthaben, die Aufladegebühr und Ihren Transaktionsverlauf zu sehen.

## Guthaben

Ihr Guthaben lautet auf US-Dollar. Skynet führt es in Cent.

Skynet gibt standardmäßig kein kostenloses Startguthaben. Laden Sie Guthaben auf, bevor Sie Ihren ersten kostenpflichtigen Durchlauf starten.

Sie laden Guthaben unter **Einstellungen → Abrechnung** auf. Sie können einen festen Betrag wählen oder einen eigenen Betrag eingeben:

| Aufladung      | Betrag, der Ihrem Guthaben gutgeschrieben wird |
| -------------- | ---------------------------------------------- |
| Feste Beträge  | 5 $, 20 $ oder 50 $                            |
| Eigener Betrag | Jeder Betrag von 5 $ bis 1.000 $               |

Der gewählte Betrag ist der Betrag, der Ihrem Guthaben gutgeschrieben wird. Die Aufladegebühr kommt hinzu. Siehe die Gebühren unten.

**Einstellungen → Nutzung** zeigt, was Sie ausgegeben haben. Der Transaktionsverlauf unter **Einstellungen → Abrechnung** listet Ihre Aufladungen und die Belastungen durch Durchläufe auf.

## Durchlaufbudgets

Jeder Durchlauf hat ein Ausgabenlimit. Das Limit ist der Höchstbetrag, den der Durchlauf ausgeben darf. Es deckt Einrichtung und Optimierung ab.

Ein Durchlauf hat vor dem Start keine Kostenschätzung. Er gibt bis zu seinem Limit aus, stoppt am Budget und behält seine beste Version. Unter [Durchläufe](/docs/concepts/runs) erfahren Sie, wie Skynet entscheidet, wann es stoppt.

Sie legen das Limit im Bildschirm **Budget** des Einreichungsassistenten im Feld **Ausgabenlimit** fest:

- **Limit setzen** ist der Standardmodus. Das Limit beginnt bei 5 $. Braucht der Durchlauf mehr als 5 $ zum Start, beginnt das Limit bei diesem Betrag.
- **Kein Limit** lässt den Durchlauf auf Ihr Guthaben zugreifen, bis er abgeschlossen ist. Der Durchlauf pausiert nie. Ist Ihr Guthaben aufgebraucht, stoppt der Durchlauf und behält, was er erzeugt hat.

### Der zum Start nötige Betrag

Der Assistent zeigt den Betrag **Zum Start nötig**. Dieser Betrag setzt sich zusammen aus:

- den Vorbehalten für die Sandboxes, die der Durchlauf vor seinem ersten Modellaufruf öffnet, plus
- einem Puffer für die ersten Modellaufrufe. Der Puffer beträgt 1 $ oder die Kosten des teuersten einzelnen Aufrufs Ihrer gewählten Modelle, wenn diese höher sind.

Der Assistent lässt Sie nicht weitermachen, wenn das Limit unter diesem Betrag liegt. Er hält Sie auch an, wenn Ihr Guthaben unter diesem Betrag liegt. Erhöhen Sie das Limit oder laden Sie Guthaben auf, um weiterzumachen.

> [!TIP]
> Wenn ein Durchlauf an seinem Budget stoppt, verwenden Sie **Limit erhöhen und fortfahren** auf der Seite des Durchlaufs. Der Durchlauf macht ab seinem letzten Checkpoint weiter.

## Reservierungen

Bevor Skynet kostenpflichtige Arbeit ausführt, reserviert es einen Teil des Limits des Durchlaufs. Skynet gibt die Reservierung frei, wenn die Arbeit abgerechnet ist, und berechnet nur die tatsächlichen Kosten.

Wenn ein Budget bereits Aktivität hat, zeigt die Karte **Ausgabenlimit** im Assistenten dessen Kontoauszug:

| Bezeichnung                       | Bedeutung                                                                          |
| --------------------------------- | ---------------------------------------------------------------------------------- |
| **Verbleibend in diesem Limit**   | Der Teil des Limits, der noch übrig ist.                                           |
| **Bisher ausgegeben**             | Die abgerechneten Kosten.                                                          |
| **Reserviert**                    | Geld, das für laufende Arbeit zurückgehalten wird. Skynet gibt es frei, wenn die Arbeit abgerechnet ist. |

## Gebühren

Skynet erhebt zwei Gebühren zusätzlich zu den Anbieterkosten.

| Gebühr                              | Aktueller Standardwert                       | Gilt für                                                 |
| ----------------------------------- | -------------------------------------------- | -------------------------------------------------------- |
| Aufladegebühr                       | 12,5 % des Betrags plus 0,35 $               | Jede Aufladung                                           |
| Nutzungsaufschlag                   | Anbieterkosten × 1,15 (Kosten plus 15 %)     | Modellaufrufe über Skynets Schlüssel und Sandbox-Rechenzeit |

### Aufladegebühr

Skynet rundet den prozentualen Anteil auf den nächsten Cent auf und addiert dann 0,35 $. Eine Aufladung von 20 $ hat zum Beispiel eine Gebühr von 2,85 $. Der Aufladebildschirm zeigt die Gebühr, bevor Sie bezahlen.

### Nutzungsaufschlag

Skynet multipliziert die Anbieterkosten mit dem Nutzungsaufschlag. Der Aufschlag gilt für Modellaufrufe, die Skynet bezahlt, und für Sandbox-Rechenzeit. Reservierungen, der zum Start nötige Betrag und die Modellpreise in der App enthalten den Aufschlag bereits.

Skynet ermittelt die Anbieterkosten eines Modellaufrufs in dieser Reihenfolge:

1. Die Kosten, die OpenRouter für den Aufruf meldet.
2. Die aktuelle Preisliste von OpenRouter für das Modell.
3. Die Preistabelle von LiteLLM.
4. Ein hoher Ersatztarif von 15 $ pro Million Eingabetoken und 75 $ pro Million Ausgabetoken. Dieser Tarif stellt sicher, dass Skynet ein unbekanntes Modell nie unter seinen Kosten berechnet.

## Ihre eigenen Anbieterschlüssel (BYOK)

Sie können Modellaufrufe über Ihren eigenen Anbieterschlüssel ausführen. Fügen Sie Schlüssel unter **Einstellungen → Anbieter** hinzu. Für OpenRouter können Sie auch **Weiter mit OpenRouter** verwenden.

Wenn ein Modellaufruf Ihren Schlüssel verwendet:

- Ihr Anbieter rechnet die Modell-Token über Ihren Schlüssel ab.
- Skynet erhebt keine Modellgebühr.
- Das Laufbudget deckt nur die Nutzung der Sandbox ab.

Gebühren eines externen Scorers oder MCP-Anbieters gehen direkt an diesen Anbieter. Sie sind nicht Teil des Ausgabenlimits des Durchlaufs.

## Sandbox-Nutzung

Ihr Ausführungscode und Ihre Metrik laufen in einer Sandbox. Skynet misst die Sandbox-Rechenzeit:

1. Bevor eine Sandbox startet, reserviert Skynet einen Betrag aus dem Limit des Durchlaufs.
2. Wenn die Sandbox fertig ist, berechnet Skynet die endgültige Nutzung, die der Sandbox-Anbieter meldet.
3. Der Nutzungsaufschlag gilt für Sandbox-Rechenzeit.

Sandbox-Rechenzeit ist Teil des Ausgabenlimits des Durchlaufs, auch wenn die Modellaufrufe Ihren eigenen Schlüssel verwenden.

## Tarife

Es gibt zwei Tarife: Free und Pro. Pro erhöht einige Limits, etwa für gleichzeitige Durchläufe und Speicher. Schlägt eine Pro-Verlängerung fehl, behält das Konto die Pro-Limits 7 Tage lang. Danach gelten die Free-Limits, bis die Zahlung erfolgreich ist. Siehe [Limits](/docs/limits).

## Schutzmechanismen der Plattform

- Eine interaktive Modellrunde, etwa im Chat mit dem KI-Assistenten, benötigt vor dem Start ein Guthaben von mindestens 0,05 $.
- Der KI-Assistent im Einreichungsassistenten ist kostenlos. Jedes Konto kann pro UTC-Tag 50 Runden mit diesem KI-Assistenten starten.

## Verwandte Seiten

- [Durchläufe](/docs/concepts/runs)
- [Einen Durchlauf mit dem Assistenten starten](/docs/guides/submit)
- [Limits](/docs/limits)
