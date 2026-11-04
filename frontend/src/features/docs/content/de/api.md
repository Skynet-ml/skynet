Mit der Skynet-HTTP-API können Skripte und Werkzeuge dasselbe tun wie die Web-App: Durchläufe starten, sie beobachten und die Ergebnisse lesen. Diese Seite zeigt, wie Sie Anfragen authentifizieren, wo die vollständige Referenz liegt, und ein vollständiges Beispiel.

> [!TIP]
> Verwenden Sie einen KI-Agenten wie Claude Code, Codex oder Cursor? Verbinden Sie ihn stattdessen über [MCP](/docs/mcp). Der Agent startet und beobachtet dann Durchläufe für Sie, mit einem Ausgabenlimit für jeden Durchlauf.

## Basis-URL

Senden Sie jede Anfrage an:

```text
https://api.skynetml.com
```

## Authentifizierung

Die API verwendet API-Tokens.

1. Öffnen Sie in Skynet **Einstellungen** und dann den Tab **API**.
2. Klicken Sie auf **Token generieren**.
3. Kopieren Sie das Token sofort. Skynet zeigt das vollständige Token nur einmal.

Senden Sie das Token im Header `Authorization` jeder Anfrage:

```http
Authorization: Bearer skyd_...
```

- Tokens beginnen mit `skyd_`.
- Jeder Nutzer hat höchstens ein gültiges Token. Ein neues Token macht das alte Token ungültig.
- Klicken Sie auf **Token widerrufen**, um ein Token ungültig zu machen.

> [!WARNING]
> Behandeln Sie das Token wie ein Passwort. Halten Sie es aus Quellcode und Logs heraus. Wie Skynet Tokens speichert, lesen Sie unter [Sicherheit](/docs/security).

## Interaktive Referenz

Die vollständige Referenz finden Sie unter [api.skynetml.com/scalar](https://api.skynetml.com/scalar). Sie listet jeden öffentlichen Endpunkt mit seinen Feldern und Antworten auf. Von dieser Seite aus können Sie Testanfragen senden.

Die Referenz liest das OpenAPI-Dokument unter `https://api.skynetml.com/openapi.public.json`. Mit dieser Datei können Sie einen Client in Ihrer Programmiersprache generieren.

## Wichtige Endpunkte

| Methode und Pfad                                | Was er tut                                                       |
| ----------------------------------------------- | ---------------------------------------------------------------- |
| `GET /models`                                   | Listet die Modelle auf, die Sie verwenden können.                |
| `GET /optimizers`                               | Listet die Optimierer auf, die zu einer Konfiguration passen.    |
| `GET /blackbox/engines`                         | Listet die nativen Engines auf und ob jede verfügbar ist.        |
| `POST /optimize` | Startet einen Durchlauf für ein DSPy-Programm. |
| `POST /blackbox/run`                            | Startet einen Durchlauf auf einem GitHub-Repository.             |
| `GET /optimizations`                            | Listet Ihre Durchläufe auf.                                      |
| `GET /optimizations/{optimization_id}`          | Gibt einen Durchlauf zurück: Status, Fortschritt, Logs und Ergebnis. |
| `GET /optimizations/{optimization_id}/artifact` | Gibt die beste Version zurück, die der Durchlauf gefunden hat.   |
| `POST /optimizations/{optimization_id}/cancel`  | Stoppt einen Durchlauf.                                          |
| `POST /serve/{optimization_id}`                 | Führt die beste Version mit einer neuen Eingabe aus.             |
| `GET /health`                                   | Zeigt, ob der Dienst läuft.                                      |

## Einen Durchlauf starten

`POST /optimize` nimmt dieselbe Konfiguration an wie der Einreichungsassistent. Das Beispiel verbessert ein DSPy-Programm, das Support-Nachrichten Labels zuordnet.

```bash tab="curl"
curl https://api.skynetml.com/optimize \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "task": {
    "objective": "Sort each support message into billing, bug or other.",
    "artifact": {
      "kind": "dspy_program",
      "starting_version": "Label this support message as billing, bug or other."
    }
  },
  "data": {
    "mode": "pairs",
    "cases": [
      { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
      { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
      {
        "input": { "message": "Do you have a Hebrew interface?" },
        "expected": { "label": "other" }
      }
    ]
  },
  "optimizer": { "choice": "auto", "budget": { "max_scorer_runs": 200 } },
  "models": {
    "task_model": { "name": "<model from GET /models>" },
    "reflection_model": { "name": "<model from GET /models>" }
  },
  "workspace": {
    "run_code": "import dspy\n\nprogram = dspy.Predict(\"message -> label\")\n\n\ndef run(artifact, input):\n    return program(message=input[\"message\"]).label\n",
    "metric_code": "def score(input, output, expected=None):\n    ok = str(output).strip().lower() == expected[\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n"
  },
  "max_cost_cents": 500
}
```

Wichtige Felder:

- `task.artifact.kind` ist `dspy_program`.
- `data.mode` ist `pairs` (Eingaben mit erwarteten Ausgaben) oder `single` (genau ein Fall).
- `optimizer.choice` ist `auto` oder ein Optimierername aus `GET /optimizers`.
- `max_cost_cents` ist das Ausgabenlimit für diesen Durchlauf, in US-Cent.
- `is_private` ist standardmäßig `false`. Setzen Sie es auf `true`, um den Durchlauf privat zu halten.

Die Regeln für `run_code` und `metric_code` lesen Sie unter [Metriken und Scorer](/docs/concepts/scorers).

### Antworten

| Status | Bedeutung                                                                                                                                                      |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet hat den Durchlauf in die Warteschlange gestellt. Der Body enthält `optimization_id` und `status`.                                                        |
| `202`  | Die Konfigurationsprüfung läuft noch. Warten Sie die Sekunden aus dem Header `Retry-After` ab. Senden Sie dann denselben Body mit demselben `Idempotency-Key`. |
| `402`  | Ihr Guthaben ist zu niedrig.                                                                                                                                   |
| `409`  | Sie haben ein Kontingent erreicht, oder die Konfigurationsprüfung ist veraltet.                                                                                |
| `422`  | Die Anfrage oder der Workspace-Code passt nicht zum Optimierer.                                                                                                |

> [!NOTE]
> Senden Sie immer einen Header `Idempotency-Key`. Senden Sie dieselbe Anfrage erneut mit demselben Schlüssel, gibt Skynet den ersten Durchlauf zurück. Es startet keinen zweiten Durchlauf.

## Einen Durchlauf beobachten

Fragen Sie den Durchlauf ab, bis sein Status endgültig ist:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Das Feld `status` hat einen dieser Werte:

| Status                             | Endgültig |
| ---------------------------------- | --------- |
| `pending`, `validating`, `running` | Nein      |
| `paused`                           | Nein      |
| `success`                          | Ja        |
| `failed`, `cancelled`, `stopped`   | Ja        |

Fragen Sie alle 10 bis 30 Sekunden ab. Ein Durchlauf kann Minuten bis Stunden dauern.

Die [API-Referenz](https://api.skynetml.com/scalar) listet jedes Feld der Durchlaufantwort auf.

## Das Ergebnis verwenden

Lautet der Status `success`, rufen Sie die beste Version ab:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Um die beste Version mit einer neuen Eingabe auszuführen, senden Sie `POST /serve/{optimization_id}`. Was jeder Score bedeutet, lesen Sie unter [Ergebnisse lesen](/docs/guides/results).

## Limits

Die API verwendet dieselben Kontingente, Ratenlimits und dieselbe Abrechnung wie die Web-App. Lesen Sie [Limits](/docs/limits) und [Abrechnung](/docs/concepts/billing).
