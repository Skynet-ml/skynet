Lo scorer dice a Skynet quanto è buona una versione del tuo repository. È una funzione Python in `scorer.py`. Ogni motore chiama la stessa funzione. Questa pagina spiega il contratto, il valore restituito e alcuni esempi.

## Il contratto

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Argomento   | Che cosa contiene                                                                |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Una cartella che contiene solo i tuoi file della metrica. Non contiene altri file del repository né codice della versione |
| `case`      | Un dict. `case["input"]` contiene i campi di input. `case["expected"]` contiene il valore atteso, oppure `None` |
| `output`    | Ciò che la voce `[run]` ha restituito per il caso, oppure `None` se il file non ha `[run]` |

Il file `optimize.toml` del repository deve avere una sezione `[run]` o una fase `[target.<name>]`. L'`entry` di `[run]` esegue una versione su un caso, nel proprio box della sandbox. Skynet rifiuta un file senza nessuna delle due, perché ogni versione otterrebbe lo stesso punteggio.

- Una voce di funzione, per esempio `entry = "serve.py:serve"`, riceve `case["input"]` come argomento. `output` è il suo valore restituito.
- Una voce di comando, per esempio `entry = ["python", "-m", "pytest", "-q"]`, riceve l'input come JSON su stdin. `output` è `{"exit_code": ..., "stdout": "..."}`.

Lo scorer gira in un box della sandbox separato. Quel box contiene solo i tuoi file della metrica. Lo scorer non può importare né eseguire il codice della versione. Vede la versione solo attraverso `output`, o attraverso `call()` verso una fase `[target.<name>]`. Importa `call` con `from skynet import call`. Leggi [Come funziona l'ottimizzazione](/docs/concepts/optimization).

## Il valore restituito

`score` restituisce un oggetto:

```python
{
    "score": 0.78,
    "feedback": "Misses short names.",
    "scores": {
        "precision": {"score": 0.90},
        "recall": {"score": 0.69, "feedback": "Misses names under 4 letters."},
    },
}
```

- `"score"` è obbligatorio. È un numero finito. Solo questo punteggio ordina le versioni.
- `"feedback"` è un testo facoltativo. È consigliato, perché GEPA e Auto imparano da esso. Senza feedback, i motori vedono solo i punteggi, e il log dell'esecuzione lo segnala una volta.
- `"scores"` è facoltativo. Associa nomi a punteggi con nome. Ogni punteggio con nome è un oggetto con il proprio `"score"` e un `"feedback"` facoltativo. Ogni motore usa ogni punteggio con nome come un obiettivo a sé.

Ogni altra forma o chiave è un errore. Il controllo mostra la parte sbagliata e la forma valida.

| Valore restituito | Valido |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | Sì |
| `{"score": 0.8}` | Sì. Non c'è feedback. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | Sì |
| `0.8` | No. Il valore non è un oggetto. |
| `(0.8, "Close. Missing the date.")` | No. Il valore non è un oggetto. |
| `{"score": "0.8"}` | No. Il punteggio è una stringa. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | No. Un punteggio con nome deve essere un oggetto. |
| `{"score": 0.8, "feedbak": "OK"}` | No. La chiave `feedbak` è sconosciuta. |

## Scrivere un feedback utile

GEPA e Auto leggono il testo del feedback per scrivere la versione successiva. Di' che cosa è sbagliato e com'è un output corretto.

| Feedback debole | Feedback utile                                                                       |
| --------------- | ------------------------------------------------------------------------------------ |
| «Sbagliato.»    | «Atteso 'billing', ottenuto 'other'. Il messaggio parla di un addebito.»             |
| «Formato errato.» | «L'output non è JSON valido. Ha una virgola finale dopo l'ultimo campo.»           |

> [!NOTE]
> L'agente di programmazione non vede mai il sorgente del tuo scorer, i valori attesi né i segreti dell'esecuzione. Vede solo i punteggi e il testo del feedback.

## Esempi

### Corrispondenza esatta

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` è un dict con il nome della tua colonna di output come chiave. Leggi [Dataset e suddivisioni](/docs/concepts/datasets).

### Eseguire i test del repository

La voce `[run]` esegue i test. Lo scorer legge il codice di uscita e l'output dei test.

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": output["stdout"][-2000:]}
```

Quando la entry `[run]` fallisce su un caso, quel caso non ha punteggio. Se l'entry non parte, supera il timeout o un segnale la ferma, oppure se una entry di funzione solleva un'eccezione o esce prima di restituire un valore, il feedback è `Run failed: …`, e Skynet non chiama lo scorer. Un codice di uscita diverso da zero di una entry di comando non è un fallimento: lo scorer lo riceve in `output`. Una versione con un caso così non ha punteggio. Non può essere la versione migliore, non raggiunge nessun `[outcome] target` e non apre nessuna pull request. Se un fallimento deve contare come un numero, intercetta il fallimento nel tuo programma, scrivi un output e lascia che lo scorer gli dia un punteggio. Leggi le [regole del repository](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md).

### Modello come giudice

Lo scorer può chiamare un modello. Importa `llm` dal modulo di supporto `skynet`. La chiamata passa per il gateway dei modelli di Skynet e va al modello dello scorer dell'esecuzione.

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from skynet import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

Lo scorer può anche usare qualsiasi client compatibile con OpenAI con le variabili d'ambiente `OPENAI_BASE_URL`, `APP_API_KEY` e `APP_MODEL`. Il gateway accetta solo il modello dello scorer dell'esecuzione.

## Chiamate ai modelli e segreti

- Le chiamate con le chiavi di Skynet vengono misurate sul budget dell'esecuzione. Con la tua chiave (BYOK), la chiamata va alla chiave del tuo provider, senza costi Skynet per il modello. Leggi [Fatturazione](/docs/concepts/billing).
- Lo scorer non riceve i segreti dell'esecuzione come variabili d'ambiente. Un segreto arriva allo scorer solo tramite una tabella `[services.<name>]` che `[metric] services` elenca, o tramite un server del file `[metric] mcp`. Scrivi il segreto negli `headers` della tabella come `${NAME}`. Lo scorer invia le sue richieste all'URL in `SKYNET_SERVICE_<NAME>_URL`. Skynet aggiunge il segreto fuori dalla sandbox.
- Skynet nasconde i valori dei segreti nell'output dello scorer e nei log.

## Limiti

| Elemento                | Valore                                                  |
| ----------------------- | ------------------------------------------------------- |
| Timeout dello scorer    | 60 secondi per impostazione predefinita, al massimo 600 |

## Pagine correlate

- [Dataset e suddivisioni](/docs/concepts/datasets)
- [Motori](/docs/concepts/engines)
- [Avviare un'esecuzione](/docs/guides/submit)
