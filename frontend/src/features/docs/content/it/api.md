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
| `POST /optimize` | Avvia un'esecuzione per un programma DSPy. |
| `POST /blackbox/run`                            | Avvia un'esecuzione su un repository GitHub.                        |
| `GET /optimizations`                            | Elenca le tue esecuzioni.                                           |
| `GET /optimizations/{optimization_id}`          | Restituisce un'esecuzione: stato, avanzamento, log e risultato.     |
| `GET /optimizations/{optimization_id}/artifact` | Restituisce la versione migliore trovata dall'esecuzione.           |
| `POST /optimizations/{optimization_id}/cancel`  | Arresta un'esecuzione.                                              |
| `POST /serve/{optimization_id}`                 | Esegue la versione migliore su un nuovo input.                      |
| `GET /health`                                   | Indica se il servizio è attivo.                                     |

## Avviare un'esecuzione

`POST /optimize` accetta la stessa configurazione della procedura guidata di invio. L'esempio migliora un programma DSPy che classifica i messaggi di assistenza in etichette.

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

Campi importanti:

- `task.artifact.kind` è `dspy_program`.
- `data.mode` è `pairs` (input con output attesi) o `single` (esattamente un caso).
- `optimizer.choice` è `auto` oppure il nome di un ottimizzatore restituito da `GET /optimizers`.
- `max_cost_cents` è il limite di spesa per questa esecuzione, in centesimi di dollaro USA.
- `is_private` è `false` per impostazione predefinita. Impostalo su `true` per mantenere privata l'esecuzione.

Leggi [Metriche e scorer](/docs/concepts/scorers) per le regole di `run_code` e `metric_code`.

### Risposte

| Stato  | Significato                                                                                                                                                         |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet ha messo in coda l'esecuzione. Il corpo contiene `optimization_id` e `status`.                                                                               |
| `202`  | Il controllo della configurazione è ancora in corso. Attendi i secondi indicati nell'intestazione `Retry-After`. Poi invia lo stesso corpo con la stessa `Idempotency-Key`. |
| `402`  | Il tuo saldo è troppo basso.                                                                                                                                        |
| `409`  | Hai raggiunto una quota, oppure il controllo della configurazione non è aggiornato.                                                                                 |
| `422`  | La richiesta o il codice del workspace non è compatibile con l'ottimizzatore.                                                                                       |

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

Per eseguire la versione migliore su un nuovo input, invia `POST /serve/{optimization_id}`. Leggi [Risultati](/docs/guides/results) per il significato di ogni punteggio.

## Limiti

L'API usa le stesse quote, gli stessi limiti di frequenza e la stessa fatturazione dell'app web. Leggi [Limiti](/docs/limits) e [Fatturazione](/docs/concepts/billing).
