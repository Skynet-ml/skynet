Die Seite des Durchlaufs zeigt den Fortschritt und die Ergebnisse eines Durchlaufs. Diese Anleitung erklärt die Tabs der Durchlaufseite, die Scores und wie Sie die beste Version exportieren oder bereitstellen.

## Tabs der Durchlaufseite

Die Seite des Durchlaufs zeigt nur die Tabs, die für den Durchlauf gelten. Einige Tabs hängen vom Pfad, vom Status und von Ihrer Erfahrungsstufe ab.

| Tab                     | Wann er erscheint                                                             | Was er enthält                                                                                   |
| ----------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| **Übersicht**           | Immer                                                                         | Fortschritt, Scores und der Score-Verlauf                                                        |
| **Nutzung**             | Ein erfolgreicher Durchlauf mit einem Programm, das Skynet bereitstellen kann | Ein Formular zum Ausführen des optimierten Programms und Integrationscode                         |
| **Artefakt** | Ein Repository-Durchlauf mit Ergebnis | Die Versionen Ihres Artefakts, mit der besten Version |
| **Daten**               | Ein abgeschlossener DSPy-Programm-Durchlauf                                   | Die Aufteilung der Daten in Trainings-, Validierungs- und Testmenge sowie die Auswertung auf dem Testset |
| **Code**                | Wenn der Durchlauf Code zum Anzeigen hat                                      | Der Ausführungscode und die Metrik                                                               |
| **Belohnung** | Ein Repository-Durchlauf, dessen Metrik Belohnungsteile angibt | Die Belohnungsteile und ihre Gewichte |
| **Artefakt** | Ein abgeschlossener DSPy-Programm-Durchlauf | Die optimierte Ausgabe und **Ergebnisse exportieren** |
| **Logs**                | Die meisten Durchläufe                                                        | Das Log des Durchlaufs                                                                           |
| **Nutzung und Kosten**  | Wenn Sie angemeldet sind                                                      | Was der Durchlauf ausgegeben hat                                                                 |
| **Geplanter Lauf** | Ein erfolgreicher Durchlauf mit einem DSPy-Programm | Ein Zeitplan, um die Optimierung erneut auszuführen |
| **Konfiguration**       | Immer                                                                         | Die Konfiguration des Durchlaufs                                                                 |

> [!NOTE]
> In der Stufe **Geführt** blendet die Seite des Durchlaufs die Tabs **Daten**, **Code**, **Logs** und **Konfiguration** aus. Ein direkter Link auf einen dieser Tabs öffnet ihn trotzdem. Die Stufe ändern Sie unter **Einstellungen → Konto**.

## Scores

Skynet wählt Versionen anhand der Validierungsfälle aus. Den endgültigen Score meldet es auf den Testfällen. Die Engines sehen die Testfälle nie. Lesen Sie [Datensätze und Aufteilungen](/docs/concepts/datasets).

Der Tab **Übersicht** zeigt:

- **Verbesserung**: die Änderung von der Ausgangsversion zur besten Version.
- **Score-Verlauf**: den Score über den Durchlauf hinweg.
- **Score-Aufschlüsselung**: jede Metrik mit den Spalten **Baseline**, **Optimiert** und **Änderung**.

Solange der Durchlauf aktiv ist, zeigt die Übersicht auch **Verstrichen**, **Verbleibend** und **Rate**.

Bei einem Repository-Durchlauf zeigt die Übersicht auch **Benannte Punktzahlen**, wenn Ihre Metrik benannte Scores meldet.

Bei einem DSPy-Programm-Durchlauf hat der Tab **Daten** eine **Auswertung auf dem Testset**. Sie zeigt für jeden Testfall den Score der Baseline und den optimierten Score.

### Wenn die beste Version die Ausgangsversion ist

Manchmal schlägt kein Kandidat die Ausgangsversion. Skynet gibt dann die Ausgangsversion zurück. Der Tab **Artefakt** teilt Ihnen mit, dass ein Regressionsschutz angewendet wurde.

Hat der Durchlauf an seinem Budget gestoppt, teilt Ihnen die Seite des Durchlaufs mit, welche Version Skynet gespeichert hat. Lesen Sie [Durchläufe](/docs/concepts/runs).

## Die beste Version exportieren

### Repository-Durchläufe

Der Tab **Artefakt** listet die **Versionen** Ihres Artefakts auf. Für jede Version können Sie:

- die Version kopieren.
- die Version als Datei herunterladen.
- bei einem Repository-Durchlauf die Version als ZIP des gesamten Repositorys herunterladen.

Sie können eine Version mit einer anderen Version vergleichen. Der Tab zeigt auch das **Feedback des Bewerters** für eine Version.

### DSPy-Programm-Durchläufe

Der Tab **Artefakt** hat einen Bereich **Ergebnisse exportieren**. Öffnen Sie das Menü **Herunterladen**. Es zeigt die Formate, die der Durchlauf hat:

| Option                | Format                                                 |
| --------------------- | ------------------------------------------------------ |
| DSPy-Programm         | `.zip`, ein eigenständiger Export, den Sie ausführen können |
| Optimiertes Programm  | `.pkl`                                                 |
| Prompt                | `.json`                                                |
| Optimierter Code      | `.py`                                                  |
| Protokolle            | `.csv`                                                 |

## Die beste Version bereitstellen

Sie können ein optimiertes DSPy-Programm aus Ihrem eigenen Code aufrufen. Der Tab **Nutzung** hat zwei Teile:

- Ein Formular. Geben Sie die Eingabefelder ein und wählen Sie **Ausführen**, um eine Vorhersage zu erhalten.
- **Integrationscode** in cURL, Python, JavaScript, Go und DSPy.

Das Programm läuft immer mit den Modelleinstellungen, mit denen es optimiert wurde. Sie müssen alle Eingabefelder angeben. Zusätzliche Felder ignoriert Skynet.

Um das Programm aufzurufen, erstellen Sie unter **Einstellungen → API** ein API-Token. Senden Sie dann eine Anfrage:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"question": "What is DSPy?"}}'
```

```python tab="Python"
import os
import uuid
import requests

response = requests.post(
    f"{os.environ['API_URL']}/serve/<optimization_id>",
    headers={
        "Authorization": f"Bearer {os.environ['APP_API_TOKEN']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={"inputs": {"question": "What is DSPy?"}},
)
print(response.json()["outputs"])
```

Ersetzen Sie `question` durch die Eingabefelder Ihres Programms. Der **Integrationscode** im Tab **Nutzung** setzt die echte URL und die Feldnamen für Sie ein.

Die Antwort enthält `outputs`, die `input_fields`, die `output_fields` und das `model_used`.

> [!TIP]
> Der Anfragetext akzeptiert auch `max_cost_cents`. Damit legen Sie fest, wie viel dieser eine Aufruf höchstens kosten darf, in Cent. Einige Durchläufe verlangen diesen Wert.

Um Teilausgaben zu erhalten, sobald sie eintreffen, senden Sie dieselbe Anfrage an `/serve/<optimization_id>/stream`. Die Antwort ist ein Stream von Server-Sent Events.

Authentifizierung und die anderen Endpunkte beschreibt [API](/docs/api).

## Die Ergebnisse teilen

Sie können einen Durchlauf mit anderen Personen teilen oder ihn in der öffentlichen Galerie „Erkunden“ listen. Lesen Sie [Teilen und veröffentlichen](/docs/guides/sharing).
