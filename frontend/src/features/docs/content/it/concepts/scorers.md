Lo scorer dice a Skynet quanto è buona una versione del tuo repository. È una funzione Python in `scorer.py`. Ogni motore chiama la stessa funzione. Questa pagina spiega il contratto, il valore restituito e alcuni esempi.

## Il contratto

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| Argomento   | Che cosa contiene                                                                |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | La cartella di una versione del repository, dopo l'esecuzione del comando di configurazione |
| `case`      | Un dict. `case["input"]` contiene i campi di input. `case["expected"]` contiene il valore atteso, oppure `None` |

Se la tua funzione riceve un solo argomento, Skynet chiama `score(repo_path)`. Usa questa forma quando lo scorer esegue i propri test e non ha bisogno di casi.

Lo scorer gira nella sandbox, nella cartella della versione. Può importare codice dal repository, eseguire un comando o chiamare `serve()` da `serve.py`. Leggi [Come funziona l'ottimizzazione](/docs/concepts/optimization).

## Il valore restituito

`score` deve restituire un punteggio con un feedback. Queste forme sono valide:

1. Una coppia: un numero, e una stringa di feedback oppure un dict con una chiave `"feedback"`.
2. Un dict con un numero in `"score"` e una stringa in `"feedback"`.

Il feedback deve essere una stringa non vuota.

Le informazioni aggiuntive possono avere anche una chiave `"scores"`. Associa nomi a punteggi con nome. Ogni punteggio con nome ha il proprio punteggio e il proprio feedback, ad esempio `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

Le informazioni aggiuntive possono avere anche una chiave `"cases"` con la stessa forma. Ogni voce è il risultato di un caso. Ogni motore usa ogni caso come un obiettivo a sé.

Se il valore restituito ha una forma diversa, il controllo fallisce e mostra il motivo.

| Valore restituito                                                               | Valido                                       |
| ------------------------------------------------------------------------------- | -------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | Sì                                           |
| `(0.8, "Close. Missing the date.")`                                             | Sì                                           |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | Sì                                |
| `0.8`                                                                           | No. Manca il feedback.                       |
| `(0.8, {})`                                                                     | No. Manca la stringa di feedback.            |
| `("0.8", {"feedback": "OK"})`                                                   | No. Il punteggio è una stringa.              |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | No. Un punteggio con nome non è un numero.  |

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

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` è un dict con il nome della tua colonna di output come chiave. Leggi [Dataset e suddivisioni](/docs/concepts/datasets).

### Eseguire i test del repository

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### Modello come giudice

Lo scorer può chiamare un modello. Importa `llm` dal modulo di supporto `skynet`. La chiamata passa per il gateway dei modelli di Skynet e va al modello dello scorer dell'esecuzione.

```python tab="scorer.py"
import importlib.util

from skynet import llm


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.serve(case["input"])
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {answer}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

Lo scorer può anche usare qualsiasi client compatibile con OpenAI con le variabili d'ambiente `OPENAI_BASE_URL`, `APP_API_KEY` e `APP_MODEL`. Il gateway accetta solo il modello dello scorer dell'esecuzione.

## Chiamate ai modelli e segreti

- Le chiamate con le chiavi di Skynet vengono misurate sul budget dell'esecuzione. Con la tua chiave (BYOK), la chiamata va alla chiave del tuo provider, senza costi Skynet per il modello. Leggi [Fatturazione](/docs/concepts/billing).
- I segreti dell'esecuzione arrivano allo scorer come variabili d'ambiente. Il nome della variabile è il nome del segreto, ad esempio `os.environ["SEARCH_API_KEY"]`.
- Skynet nasconde i valori dei segreti nell'output dello scorer e nei log.

## Limiti

| Elemento                | Valore                                                  |
| ----------------------- | ------------------------------------------------------- |
| Timeout dello scorer    | 60 secondi per impostazione predefinita, al massimo 600 |

## Pagine correlate

- [Dataset e suddivisioni](/docs/concepts/datasets)
- [Motori](/docs/concepts/engines)
- [Avviare un'esecuzione](/docs/guides/submit)
