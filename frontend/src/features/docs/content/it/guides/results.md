La pagina dell'esecuzione mostra l'avanzamento e i risultati di un'esecuzione. Questa guida spiega le schede della pagina dell'esecuzione, i punteggi e come esportare o servire la versione migliore.

## Schede della pagina dell'esecuzione

La pagina dell'esecuzione mostra solo le schede pertinenti all'esecuzione. Alcune schede dipendono dallo stato e dal tuo livello di esperienza.

| Scheda                     | Quando compare                                                   | Che cosa contiene                                                                  |
| -------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **Panoramica**             | Sempre                                                           | Avanzamento, punteggi e cronologia dei punteggi                                    |
| **Utilizzo**               | Un'esecuzione riuscita il cui repository ha un file `serve.py`   | Un modulo per chiamare la versione migliore e il codice di integrazione            |
| **Artefatto**              | Un'esecuzione con un risultato                                   | Le versioni del tuo repository, con la versione migliore                           |
| **Dati**                   | Un'esecuzione terminata con casi                                 | I dati suddivisi in set di addestramento, convalida e test                         |
| **Codice**                 | Quando l'esecuzione ha codice da mostrare                        | Lo scorer                                                                          |
| **Ricompensa**             | Un'esecuzione il cui scorer dichiara parti di ricompensa         | Le componenti della ricompensa e i loro pesi                                       |
| **Log**                    | La maggior parte delle esecuzioni                                | Il log dell'esecuzione                                                             |
| **Utilizzo e costi**       | Quando hai effettuato l'accesso                                  | Quanto ha speso l'esecuzione                                                       |
| **Esecuzione pianificata** | Un'esecuzione riuscita                                           | Una pianificazione per eseguire di nuovo l'ottimizzazione dalla versione migliore |
| **Configurazione**         | Sempre                                                           | La configurazione dell'esecuzione                                                  |

> [!NOTE]
> Al livello **Guidato**, la pagina dell'esecuzione nasconde le schede **Dati**, **Codice**, **Log** e **Configurazione**. Un link diretto a una di queste schede la apre comunque. Cambia il livello in **Impostazioni → Account**.

## Punteggi

Skynet seleziona le versioni sui casi di convalida. Riporta il punteggio finale sui casi di test. I motori non vedono mai i casi di test. Leggi [Dataset e suddivisioni](/docs/concepts/datasets).

La scheda **Panoramica** mostra:

- **Miglioramento**: la variazione dalla versione iniziale alla versione migliore.
- **Progressione del punteggio**: il punteggio nel corso dell'esecuzione.
- **Dettaglio del punteggio**: ogni punteggio, con le colonne **Baseline**, **Ottimizzato** e **Variazione**.

Mentre l'esecuzione è attiva, la panoramica mostra anche **Trascorso**, **Rimanente** e **Velocità**.

La panoramica mostra anche **Punteggi con nome** quando il tuo scorer riporta punteggi con nome.

### Quando la versione migliore è la versione iniziale

A volte nessun candidato supera la versione iniziale. In questo caso Skynet restituisce la versione iniziale. La scheda **Artefatto** ti informa che è stata applicata una protezione contro le regressioni.

Se l'esecuzione si è fermata al suo budget, la pagina dell'esecuzione ti indica quale versione Skynet ha salvato. Leggi [Esecuzioni](/docs/concepts/runs).

## Esportare la versione migliore

La scheda **Artefatto** elenca le **Versioni** del tuo repository. Per ogni versione puoi:

- Copiare la versione.
- Scaricare la versione come file.
- Scaricare la versione come zip dell'intero repository.

Puoi confrontare una versione con un'altra. La scheda mostra anche il **Feedback dello scorer** di una versione.

### Branch in un nuovo repository

Quando Skynet crea il repository per l'esecuzione, invia ogni candidato a un proprio branch, `skynet/candidate-<version id>`. Il branch `skynet/best` punta sempre alla versione migliore. Si sposta quando compare una versione migliore.

## Servire la versione migliore

Puoi chiamare la versione migliore dal tuo codice. Il repository deve avere nella radice un file `serve.py` che definisce `serve(input)`:

```python tab="serve.py"
def serve(input):
    return {"label": classify(input["message"])}
```

- `input` è il dict dei campi di input della richiesta.
- Il valore restituito deve essere serializzabile in JSON. Un dict diventa `outputs`. Qualsiasi altro valore diventa `{"output": value}`.
- `serve.py` può chiamare un modello con `llm` dal modulo di supporto `skynet`. Non riceve i segreti dell'esecuzione.

Skynet esegue la versione migliore nella stessa sandbox protetta dell'esecuzione. La scheda **Utilizzo** ha due parti:

- Un modulo. Inserisci i campi di input e seleziona **Esegui** per ottenere un risultato.
- Il **Codice di integrazione** in cURL, Python, JavaScript e Go.

Per chiamare la versione migliore, crea un token API in **Impostazioni → API**. Poi invia una richiesta. L'header `Idempotency-Key` è obbligatorio:

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

Sostituisci `message` con i campi di input della tua esecuzione. Il **Codice di integrazione** nella scheda **Utilizzo** inserisce per te l'URL reale e i nomi dei campi.

La risposta contiene `outputs`, gli `input_fields`, gli `output_fields`, il `model_used`, `cents_charged` e `budget`.

> [!TIP]
> Ogni chiamata viene addebitata a sé, sul saldo di chi chiama. `max_cost_cents` imposta il costo massimo di questa singola chiamata, in centesimi. Se invii di nuovo la stessa `Idempotency-Key`, Skynet non addebita di nuovo la chiamata.

Leggi [API](/docs/api) per l'autenticazione e gli altri endpoint.

## Condividere i risultati

Puoi condividere un'esecuzione con altre persone, oppure elencarla nella galleria pubblica Esplora. Leggi [Condividere e pubblicare](/docs/guides/sharing).
