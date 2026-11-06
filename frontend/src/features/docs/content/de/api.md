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
| `POST /blackbox/run`                            | Startet einen Durchlauf auf einem GitHub-Repository.             |
| `GET /optimizations`                            | Listet Ihre Durchläufe auf.                                      |
| `GET /optimizations/{optimization_id}`          | Gibt einen Durchlauf zurück: Status, Fortschritt, Logs und Ergebnis. |
| `GET /optimizations/{optimization_id}/artifact` | Gibt die beste Version zurück, die der Durchlauf gefunden hat.   |
| `POST /optimizations/{optimization_id}/cancel`  | Stoppt einen Durchlauf.                                          |
| `POST /serve/{optimization_id}`                 | Führt die beste Version mit einer neuen Eingabe aus.             |
| `GET /health`                                   | Zeigt, ob der Dienst läuft.                                      |

## Einen Durchlauf starten

Skynet ist Optimize Anything: Jeder Durchlauf optimiert ein Repository. `POST /blackbox/run` nimmt dieselbe Konfiguration an wie der Einreichungsassistent. Das Beispiel verbessert ein Repository, das Support-Nachrichten Labels zuordnet.

```bash tab="curl"
curl https://api.skynetml.com/blackbox/run \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "objective": "Sort each support message into billing, bug or other.",
  "recipe": "repo",
  "target": {
    "kind": "repo",
    "repo": {
      "provider": "github",
      "repository": "your-org/support-labels",
      "branch": "main",
      "editable_paths": ["serve.py", "prompts/"]
    }
  },
  "cases": [
    { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
    { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
    {
      "input": { "message": "Do you have a Hebrew interface?" },
      "expected": { "label": "other" }
    }
  ],
  "input_fields": ["message"],
  "expected_field": "label",
  "scorer": {
    "kind": "python",
    "metric_code": "import importlib.util\n\n\ndef score(repo_path, case):\n    spec = importlib.util.spec_from_file_location(\"serve\", f\"{repo_path}/serve.py\")\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    ok = str(module.serve(case[\"input\"])).strip().lower() == case[\"expected\"][\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n",
    "model": { "name": "<model from GET /models>" }
  },
  "strategy": { "mode": "auto" },
  "budget": { "max_scorer_runs": 200 },
  "reflection_model_config": { "name": "<model from GET /models>" },
  "max_cost_cents": 500
}
```

Wichtige Felder:

- `target.repo.repository` ist das GitHub-Repository als `owner/name`. Verbinden Sie zuerst GitHub unter **Einstellungen → Connectors**.
- `target.repo.editable_paths` listet die Dateien und Ordner auf, die der Optimierer ändern darf. `"."` bedeutet das gesamte Repository.
- `target.repo.commit` ist optional. Es ist ein SHA mit 40 Zeichen. Ohne ihn verwendet Skynet den neuesten Commit auf dem Branch.
- `target.repo.secrets` ist optional. Jedes Secret hat einen `name` und einen `value`. Es erreicht Ihren Code und Ihren Scorer als Umgebungsvariable.
- `scorer.metric_code` definiert `score(repo_path, case)`. `scorer.model` ist das Modell, das Ihr Code und Ihr Scorer über das Modell-Gateway aufrufen können.
- `strategy.mode` ist `auto` (der Standard) oder `single`. Setzen Sie bei `single` `strategy.engine` auf einen Engine-Namen aus `GET /blackbox/engines`.
- `max_cost_cents` ist das Ausgabenlimit für diesen Durchlauf, in US-Cent.
- `is_private` ist standardmäßig `false`. Setzen Sie es auf `true`, um den Durchlauf privat zu halten.

Die Regeln für `metric_code` lesen Sie unter [Metriken und Scorer](/docs/concepts/scorers).

### Antworten

| Status | Bedeutung                                                                                                                                                      |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet hat den Durchlauf in die Warteschlange gestellt. Der Body enthält `optimization_id` und `status`.                                                        |
| `400`  | Das Ziel ist kein Repository, oder der Scorer lässt sich nicht laden.                                                                                          |
| `202`  | Die Konfigurationsprüfung läuft noch. Warten Sie die Sekunden aus dem Header `Retry-After` ab. Senden Sie dann denselben Body mit demselben `Idempotency-Key`. |
| `402`  | Ihr Guthaben ist zu niedrig.                                                                                                                                   |
| `409`  | Sie haben ein Kontingent erreicht, oder die Konfigurationsprüfung ist veraltet.                                                                                |
| `422`  | Die Anfrage passt nicht zum Optimierer.                                                                                                                        |

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

Um die beste Version mit einer neuen Eingabe auszuführen, senden Sie `POST /serve/{optimization_id}` mit einem Header `Idempotency-Key`. Das Repository muss eine Datei `serve.py` mit `def serve(input)` haben. Die Anfrage, die Antwort und was jeder Score bedeutet, lesen Sie unter [Ergebnisse lesen](/docs/guides/results).

## Limits

Die API verwendet dieselben Kontingente, Ratenlimits und dieselbe Abrechnung wie die Web-App. Lesen Sie [Limits](/docs/limits) und [Abrechnung](/docs/concepts/billing).
