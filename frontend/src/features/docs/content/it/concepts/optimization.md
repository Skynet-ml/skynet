Skynet è Optimize Anything: ogni esecuzione ottimizza un repository. Un motore propone nuove versioni del repository, il tuo scorer assegna un punteggio a ogni versione e Skynet conserva la migliore.

## Il contratto comune

Ogni esecuzione ha un repository e uno scorer. Lo scorer è una funzione Python che riceve il percorso di una versione del repository e un caso.

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.serve(case["input"])
    ok = output == case["expected"]
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `repo_path` è la cartella di una versione del repository, dopo l'esecuzione del comando di configurazione.
- `case` è un dict. `case["input"]` contiene i campi di input di un caso. `case["expected"]` contiene il valore atteso, oppure `None` quando il caso non ne ha.
- `score` restituisce una coppia: un numero e un dict con una stringa `"feedback"`.

Il repository ha un file `serve.py` nella radice con `def serve(input)`. Lo scorer può chiamarlo, e Skynet lo usa per servire la versione migliore dopo l'esecuzione.

> [!NOTE]
> L'agente di programmazione che scrive le versioni non può vedere il sorgente dello scorer, i valori attesi né i segreti dell'esecuzione.

Leggi [Metriche e scorer](/docs/concepts/scorers) per il contratto completo dello scorer.

## Chiamate ai modelli dal tuo codice

Il codice del tuo repository e il tuo scorer possono chiamare un modello tramite il gateway dei modelli di Skynet. La sandbox fornisce un endpoint compatibile con OpenAI nelle variabili d'ambiente:

| Variabile            | Che cosa contiene                                                          |
| -------------------- | -------------------------------------------------------------------------- |
| `OPENAI_BASE_URL`    | L'URL del gateway. `APP_GATEWAY_URL` contiene lo stesso valore.            |
| `APP_API_KEY`        | Un token solo per questa esecuzione. Non è una chiave del provider.        |
| `APP_MODEL`          | Il nome del modello da inviare. È il modello dello scorer dell'esecuzione. |
| `ANTHROPIC_BASE_URL` | L'URL del gateway per gli SDK di Anthropic.                                |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["APP_API_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["APP_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- Le chiamate con le chiavi di Skynet vengono misurate sul budget dell'esecuzione.
- Con la tua chiave (BYOK), la chiamata va alla chiave del tuo provider. Skynet non applica costi per il modello.
- Il gateway rifiuta le richieste per qualsiasi altro modello.
- I segreti dell'esecuzione arrivano al tuo codice e al tuo scorer come variabili d'ambiente. Il nome della variabile è il nome del segreto. Skynet nasconde i valori dei segreti nell'output.

La sandbox non ha altri accessi alla rete.

## Motori

Ogni motore lavora sui repository:

- **Auto** è il valore predefinito. Prova diversi motori, poi continua GEPA dalla versione migliore.
- **GEPA** fa evolvere le versioni tramite riflessione e mantiene un fronte di Pareto.
- **Best-of-N** chiede al modello di riflessione proposte indipendenti e conserva la migliore.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** e **ShinkaEvolve** usano un agente di programmazione per proporre versioni.

I motori modificano solo i file nei percorsi che consenti. Leggi [Motori](/docs/concepts/engines) per ogni motore e le sue impostazioni.

## Auto

**Auto** è la scelta predefinita nella procedura guidata, per ogni esecuzione.

1. Auto esplora con cinque motori: GEPA, AutoResearch, Meta-Harness, AutoSaddler e ShinkaEvolve.
2. Sceglie la versione migliore che hanno trovato.
3. Continua GEPA da quella versione finché il budget non si esaurisce.

Auto legge il testo del feedback del tuo scorer.

## Da dove viene la versione iniziale

| La tua situazione                         | Usa                                                                                           |
| ----------------------------------------- | --------------------------------------------------------------------------------------------- |
| Hai del codice in un repository GitHub    | **Usa un repository**. Il codice del branch o del commit che scegli è il punto di partenza.   |
| Hai solo un'attività e dei dati           | **Crea un nuovo repository**. Skynet scrive una prima soluzione. Quella è il punto di partenza. |

> [!WARNING]
> GEPA e Auto imparano dal testo del feedback. Se il tuo feedback è sempre la stessa stringa, questi ottimizzatori hanno meno informazioni su cui lavorare.

## Pagine correlate

- [Motori](/docs/concepts/engines)
- [Dataset e suddivisioni](/docs/concepts/datasets)
- [Metriche e scorer](/docs/concepts/scorers)
- [Avviare un'esecuzione](/docs/guides/submit)
