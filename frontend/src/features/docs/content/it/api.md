L'API HTTP di Skynet permette a script e strumenti di fare ciò che fa l'app web: avviare esecuzioni, seguirle e leggerne i risultati. Questa pagina mostra come firmare le richieste, dove trovare il riferimento completo e un esempio completo.

> [!TIP]
> Usi un agente IA come Claude Code, Codex o Cursor? Collegalo tramite [MCP](/docs/mcp). L'agente avvia e segue le esecuzioni al posto tuo, con un limite di spesa per ogni esecuzione.

## URL di base

Invia ogni richiesta a:

```text
https://api.skynetml.com
```

## Autenticazione

L'API usa token API.

1. Apri **Impostazioni** in Skynet, poi apri la scheda **API**.
2. Fai clic su **Genera token**.
3. Copia subito il token. Skynet mostra il token completo una sola volta.

Invia il token nell'intestazione `Authorization` di ogni richiesta:

```http
Authorization: Bearer skyd_...
```

- I token iniziano con `skyd_`.
- Ogni utente ha al massimo un token valido. Un nuovo token disattiva quello vecchio.
- Fai clic su **Revoca token** per disattivare un token.

> [!WARNING]
> Tratta il token come una password. Tienilo fuori dal codice sorgente e dai log. Leggi [Sicurezza](/docs/security) per sapere come Skynet conserva i token.

## Riferimento interattivo

Il riferimento completo è su [api.skynetml.com/scalar](https://api.skynetml.com/scalar). Elenca ogni endpoint pubblico, i suoi campi e le sue risposte. Da quella pagina puoi inviare richieste di prova.

Il riferimento legge il documento OpenAPI all'indirizzo `https://api.skynetml.com/openapi.public.json`. Usa quel file per generare un client nel tuo linguaggio.

## Endpoint principali

| Metodo e percorso                               | Che cosa fa                                                         |
| ----------------------------------------------- | ------------------------------------------------------------------- |
| `GET /models`                                   | Elenca i modelli che puoi usare.                                    |
| `GET /optimizers`                               | Elenca gli ottimizzatori adatti a una configurazione.               |
| `GET /blackbox/engines`                         | Elenca i motori nativi e indica se ciascuno è disponibile.          |
| `POST /blackbox/run`                            | Avvia un'esecuzione su un repository GitHub.                        |
| `GET /optimizations`                            | Elenca le tue esecuzioni.                                           |
| `GET /optimizations/{optimization_id}`          | Restituisce un'esecuzione: stato, avanzamento, log e risultato.     |
| `GET /optimizations/{optimization_id}/artifact` | Restituisce la versione migliore trovata dall'esecuzione.           |
| `POST /optimizations/{optimization_id}/cancel`  | Arresta un'esecuzione.                                              |
| `POST /serve/{optimization_id}`                 | Esegue la versione migliore su un nuovo input.                      |
| `GET /health`                                   | Indica se il servizio è attivo.                                     |

## Avviare un'esecuzione

Skynet è Optimize Anything: ogni esecuzione ottimizza un repository. `POST /blackbox/run` accetta la stessa configurazione della procedura guidata di invio. L'esempio migliora un repository che classifica i messaggi di assistenza in etichette.

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

Campi importanti:

- `target.repo.repository` è il repository GitHub nel formato `owner/name`. Prima collega GitHub in **Impostazioni → Connettori**.
- `target.repo.editable_paths` elenca i file e le cartelle che l'ottimizzatore può modificare. `"."` indica l'intero repository.
- `target.repo.commit` è facoltativo. È uno SHA di 40 caratteri. Senza di esso, Skynet usa l'ultimo commit del branch.
- `target.repo.secrets` è facoltativo. Ogni segreto ha un `name` e un `value`. Arriva al tuo codice e al tuo scorer come variabile d'ambiente.
- `scorer.metric_code` definisce `score(repo_path, case)`. `scorer.model` è il modello che il tuo codice e il tuo scorer possono chiamare tramite il gateway dei modelli.
- `strategy.mode` è `auto` (il valore predefinito) o `single`. Con `single`, imposta `strategy.engine` sul nome di un motore restituito da `GET /blackbox/engines`.
- `max_cost_cents` è il limite di spesa per questa esecuzione, in centesimi di dollaro USA.
- `is_private` è `false` per impostazione predefinita. Impostalo su `true` per mantenere privata l'esecuzione.

Leggi [Metriche e scorer](/docs/concepts/scorers) per le regole di `metric_code`.

### Risposte

| Stato  | Significato                                                                                                                                                         |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet ha messo in coda l'esecuzione. Il corpo contiene `optimization_id` e `status`.                                                                               |
| `400`  | La destinazione non è un repository, oppure lo scorer non si carica.                                                                                                |
| `202`  | Il controllo della configurazione è ancora in corso. Attendi i secondi indicati nell'intestazione `Retry-After`. Poi invia lo stesso corpo con la stessa `Idempotency-Key`. |
| `402`  | Il tuo saldo è troppo basso.                                                                                                                                        |
| `409`  | Hai raggiunto una quota, oppure il controllo della configurazione non è aggiornato.                                                                                 |
| `422`  | La richiesta non è compatibile con l'ottimizzatore.                                                                                       |

> [!NOTE]
> Invia sempre un'intestazione `Idempotency-Key`. Se invii di nuovo la stessa richiesta con la stessa chiave, Skynet restituisce la prima esecuzione. Non avvia una seconda esecuzione.

## Seguire un'esecuzione

Interroga l'esecuzione finché il suo stato non è finale:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Il campo `status` assume uno di questi valori:

| Stato                              | Finale |
| ---------------------------------- | ------ |
| `pending`, `validating`, `running` | No     |
| `paused`                           | No     |
| `success`                          | Sì     |
| `failed`, `cancelled`, `stopped`   | Sì     |

Interroga ogni 10-30 secondi. Un'esecuzione può durare da pochi minuti a diverse ore.

Il [riferimento API](https://api.skynetml.com/scalar) elenca ogni campo della risposta di un'esecuzione.

## Usare il risultato

Quando lo stato è `success`, recupera la versione migliore:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Per eseguire la versione migliore su un nuovo input, invia `POST /serve/{optimization_id}` con un'intestazione `Idempotency-Key`. Il repository deve avere un file `serve.py` con `def serve(input)`. Leggi [Risultati](/docs/guides/results) per la richiesta, la risposta e il significato di ogni punteggio.

## Limiti

L'API usa le stesse quote, gli stessi limiti di frequenza e la stessa fatturazione dell'app web. Leggi [Limiti](/docs/limits) e [Fatturazione](/docs/concepts/billing).
