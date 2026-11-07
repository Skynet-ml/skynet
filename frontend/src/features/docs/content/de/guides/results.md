Die Seite des Durchlaufs zeigt den Fortschritt und die Ergebnisse eines Durchlaufs. Diese Anleitung erklärt die Tabs der Durchlaufseite, die Scores und wie Sie die beste Version exportieren oder bereitstellen.

## Tabs der Durchlaufseite

Die Seite des Durchlaufs zeigt nur die Tabs, die für den Durchlauf gelten. Einige Tabs hängen vom Status und von Ihrer Erfahrungsstufe ab.

| Tab                     | Wann er erscheint                                                  | Was er enthält                                                                  |
| ----------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| **Übersicht**           | Immer                                                              | Fortschritt, Scores und der Score-Verlauf                                       |
| **Nutzung**             | Ein erfolgreicher Durchlauf, dessen Repository eine Datei `serve.py` hat | Ein Formular zum Aufrufen der besten Version und Integrationscode         |
| **Artefakt**            | Ein Durchlauf mit Ergebnis                                         | Die Versionen Ihres Repositorys, mit der besten Version                         |
| **Daten**               | Ein abgeschlossener Durchlauf mit Fällen                           | Die Aufteilung der Daten in Trainings-, Validierungs- und Testmenge             |
| **Code**                | Wenn der Durchlauf Code zum Anzeigen hat                           | Der Scorer                                                                      |
| **Belohnung**           | Ein Durchlauf, dessen Scorer Belohnungsteile angibt                | Die Belohnungsteile und ihre Gewichte                                           |
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

Hat Skynet das Repository für den Durchlauf erstellt, pusht es jeden Kandidaten auf einen eigenen Branch, `skynet/candidate-<version id>`. Der Branch `skynet/best` zeigt immer auf die beste Version. Er bewegt sich, wenn eine bessere Version erscheint.

## Die beste Version bereitstellen

Sie können die beste Version aus Ihrem eigenen Code aufrufen. Das Repository muss im Stammverzeichnis eine Datei `serve.py` haben, die `serve(input)` definiert:

```python tab="serve.py"
def serve(input):
    return {"label": classify(input["message"])}
```

- `input` ist das Dict der Eingabefelder aus der Anfrage.
- Der Rückgabewert muss in JSON serialisierbar sein. Ein Dict wird zu `outputs`. Jeder andere Wert wird zu `{"output": value}`.
- `serve.py` kann mit `llm` aus dem Hilfsmodul `skynet` ein Modell aufrufen. Es bekommt die Secrets des Laufs nicht.

Skynet führt die beste Version in derselben geschützten Sandbox aus wie den Durchlauf. Der Tab **Nutzung** hat zwei Teile:

- Ein Formular. Geben Sie die Eingabefelder ein und wählen Sie **Ausführen**, um ein Ergebnis zu erhalten.
- **Integrationscode** in cURL, Python, JavaScript und Go.

Um die beste Version aufzurufen, erstellen Sie unter **Einstellungen → API** ein API-Token. Senden Sie dann eine Anfrage. Der Header `Idempotency-Key` ist erforderlich:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5}'
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
    json={"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5},
)
print(response.json()["outputs"])
```

Ersetzen Sie `message` durch die Eingabefelder Ihres Durchlaufs. Der **Integrationscode** im Tab **Nutzung** setzt die echte URL und die Feldnamen für Sie ein.

Die Antwort enthält `outputs`, die `input_fields`, die `output_fields`, das `model_used`, `cents_charged` und `budget`.

> [!TIP]
> Jeder Aufruf wird einzeln abgerechnet, vom Guthaben des Aufrufers. `max_cost_cents` legt fest, wie viel dieser eine Aufruf höchstens kosten darf, in Cent. Senden Sie denselben `Idempotency-Key` erneut, berechnet Skynet den Aufruf nicht noch einmal.

Authentifizierung und die anderen Endpunkte beschreibt [API](/docs/api).

## Die Ergebnisse teilen

Sie können einen Durchlauf mit anderen Personen teilen oder ihn in der öffentlichen Galerie „Erkunden“ listen. Lesen Sie [Teilen und veröffentlichen](/docs/guides/sharing).
