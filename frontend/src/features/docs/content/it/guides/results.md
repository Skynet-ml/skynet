La pagina dell'esecuzione mostra l'avanzamento e i risultati di un'esecuzione. Questa guida spiega le schede della pagina dell'esecuzione, i punteggi e come esportare o servire la versione migliore.

## Schede della pagina dell'esecuzione

La pagina dell'esecuzione mostra solo le schede pertinenti all'esecuzione. Alcune schede dipendono dal percorso, dallo stato e dal tuo livello di esperienza.

| Scheda                  | Quando compare                                                       | Che cosa contiene                                                                       |
| ----------------------- | -------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| **Panoramica**          | Sempre                                                               | Avanzamento, punteggi e cronologia dei punteggi                                         |
| **Utilizzo**            | Un'esecuzione riuscita con un programma che Skynet può servire       | Un modulo per eseguire il programma ottimizzato e il codice di integrazione             |
| **Artefatto** | Un'esecuzione su repository con un risultato | Le versioni del tuo artefatto, con la versione migliore |
| **Dati**                | Un'esecuzione di un programma DSPy terminata                         | I dati suddivisi in set di addestramento, convalida e test, e la valutazione sul set di test |
| **Codice**              | Quando l'esecuzione ha codice da mostrare                            | Il codice di esecuzione e la metrica                                                    |
| **Ricompensa** | Un'esecuzione su repository la cui metrica dichiara parti di ricompensa | Le componenti della ricompensa e i loro pesi |
| **Artefatto** | Un'esecuzione di un programma DSPy terminata | L'output ottimizzato ed **Esporta i risultati** |
| **Log**                 | La maggior parte delle esecuzioni                                    | Il log dell'esecuzione                                                                  |
| **Utilizzo e costi**    | Quando hai effettuato l'accesso                                      | Quanto ha speso l'esecuzione                                                            |
| **Esecuzione pianificata** | Un'esecuzione riuscita di un programma DSPy | Una pianificazione per eseguire di nuovo l'ottimizzazione |
| **Configurazione**      | Sempre                                                               | La configurazione dell'esecuzione                                                       |

> [!NOTE]
> Al livello **Guidato**, la pagina dell'esecuzione nasconde le schede **Dati**, **Codice**, **Log** e **Configurazione**. Un link diretto a una di queste schede la apre comunque. Cambia il livello in **Impostazioni → Account**.

## Punteggi

Skynet seleziona le versioni sui casi di convalida. Riporta il punteggio finale sui casi di test. I motori non vedono mai i casi di test. Leggi [Dataset e suddivisioni](/docs/concepts/datasets).

La scheda **Panoramica** mostra:

- **Miglioramento**: la variazione dalla versione iniziale alla versione migliore.
- **Progressione del punteggio**: il punteggio nel corso dell'esecuzione.
- **Dettaglio del punteggio**: ogni metrica, con le colonne **Baseline**, **Ottimizzato** e **Variazione**.

Mentre l'esecuzione è attiva, la panoramica mostra anche **Trascorso**, **Rimanente** e **Velocità**.

Per un'esecuzione su repository, la panoramica mostra anche **Punteggi con nome** quando la tua metrica riporta punteggi con nome.

Per un'esecuzione di un programma DSPy, la scheda **Dati** contiene una **Valutazione sul set di test**. Mostra il punteggio di partenza e il punteggio ottimizzato per ogni caso di test.

### Quando la versione migliore è la versione iniziale

A volte nessun candidato supera la versione iniziale. In questo caso Skynet restituisce la versione iniziale. La scheda **Artefatto** ti informa che è stata applicata una protezione contro le regressioni.

Se l'esecuzione si è fermata al suo budget, la pagina dell'esecuzione ti indica quale versione Skynet ha salvato. Leggi [Esecuzioni](/docs/concepts/runs).

## Esportare la versione migliore

### Esecuzioni su repository

La scheda **Artefatto** elenca le **Versioni** del tuo artefatto. Per ogni versione puoi:

- Copiare la versione.
- Scaricare la versione come file.
- Per un'esecuzione su repository, scaricare la versione come archivio zip dell'intero repository.

Puoi confrontare una versione con un'altra. La scheda mostra anche il **Feedback dello scorer** di una versione.

### Esecuzioni di programmi DSPy

La scheda **Artefatto** contiene un pannello **Esporta i risultati**. Apri il menu **Scarica**. Mostra i formati disponibili per l'esecuzione:

| Opzione                 | Formato                                                   |
| ----------------------- | --------------------------------------------------------- |
| Programma DSPy          | `.zip`, un'esportazione autonoma che puoi eseguire        |
| Programma ottimizzato   | `.pkl`                                                    |
| Prompt                  | `.json`                                                   |
| Codice ottimizzato      | `.py`                                                     |
| Log                     | `.csv`                                                    |

## Servire la versione migliore

Puoi chiamare un programma DSPy ottimizzato dal tuo codice. La scheda **Utilizzo** ha due parti:

- Un modulo. Inserisci i campi di input e seleziona **Esegui** per ottenere una previsione.
- Il **Codice di integrazione** in cURL, Python, JavaScript, Go e DSPy.

Il programma usa sempre le impostazioni del modello con cui è stato ottimizzato. Devi fornire tutti i campi di input. Skynet ignora i campi in più.

Per chiamare il programma, crea un token API in **Impostazioni → API**. Poi invia una richiesta:

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

Sostituisci `question` con i campi di input del tuo programma. Il **Codice di integrazione** nella scheda **Utilizzo** inserisce per te l'URL reale e i nomi dei campi.

La risposta contiene `outputs`, gli `input_fields`, gli `output_fields` e il `model_used`.

> [!TIP]
> Il corpo della richiesta accetta anche `max_cost_cents`. Imposta il costo massimo di questa singola chiamata, in centesimi. Alcune esecuzioni lo richiedono.

Per ricevere gli output parziali man mano che arrivano, invia la stessa richiesta a `/serve/<optimization_id>/stream`. La risposta è un flusso di server-sent events.

Leggi [API](/docs/api) per l'autenticazione e gli altri endpoint.

## Condividere i risultati

Puoi condividere un'esecuzione con altre persone, oppure elencarla nella galleria pubblica Esplora. Leggi [Condividere e pubblicare](/docs/guides/sharing).
