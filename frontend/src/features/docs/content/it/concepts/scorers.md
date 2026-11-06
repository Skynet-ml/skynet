La metrica indica a Skynet quanto è buono un output. È una funzione Python in `metric.py`. Ogni motore chiama la stessa funzione. Questa pagina descrive il contratto, il valore restituito e alcuni esempi.

## Il contratto

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argomento  | Che cosa contiene                                                     |
| ---------- | --------------------------------------------------------------------- |
| `input`    | Un dict con i campi di input del caso                                 |
| `output`   | Ciò che la tua `run(artifact, input)` ha restituito per il caso       |
| `expected` | Il valore atteso del caso, oppure `None` se il caso non ne ha uno     |

Se la tua funzione accetta solo due argomenti, Skynet chiama `score(input, output)`.

Per un programma DSPy, Skynet adatta la chiamata della metrica DSPy a questa stessa funzione. Scrivi una sola metrica per ogni tipo di artefatto. Leggi [Come funziona l'ottimizzazione](/docs/concepts/optimization).

## Il valore restituito

`score` deve restituire una coppia (una tupla o una lista di due elementi):

1. **Il punteggio.** Un numero. L'ottimizzatore cerca di aumentarlo.
2. **Le informazioni aggiuntive.** Un dict. Deve avere una chiave `"feedback"` con un valore stringa.

Le informazioni aggiuntive possono avere anche una chiave `"scores"`. Associa nomi a numeri, ad esempio `{"format": 1.0, "accuracy": 0.5}`.

Se il valore restituito ha una forma diversa, il controllo del contratto non supera **Valore restituito dalla metrica**.

| Valore restituito                                      | Valido                                              |
| ------------------------------------------------------ | --------------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Sì                                                  |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Sì                                                  |
| `0.8`                                                  | No. Mancano le informazioni aggiuntive.             |
| `(0.8, "Close.")`                                      | No. Le informazioni aggiuntive non sono un dict.    |
| `(0.8, {})`                                            | No. Manca la stringa di feedback.                   |
| `("0.8", {"feedback": "OK"})`                          | No. Il punteggio è una stringa.                     |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | No. Un punteggio con nome non è un numero.          |

## Scrivere un feedback utile

GEPA, DSPy GEPA e Auto leggono il testo del feedback per scrivere la versione successiva. Indica che cosa è sbagliato e com'è un output corretto.

| Feedback debole | Feedback utile                                                                       |
| --------------- | ------------------------------------------------------------------------------------ |
| «Sbagliato.»    | «Atteso 'billing', ottenuto 'other'. Il messaggio parla di un addebito.»             |
| «Formato errato.» | «L'output non è JSON valido. Ha una virgola finale dopo l'ultimo campo.»           |

> [!NOTE]
> L'ottimizzatore non vede mai il sorgente della tua metrica. Con un ottimizzatore DSPy, il modello di riflessione vede solo il testo del feedback. I tuoi valori attesi restano nella metrica.

## Esempi

### Corrispondenza esatta

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` è un dict con il nome della tua colonna Output come chiave. Leggi [Dataset e suddivisioni](/docs/concepts/datasets).

### Più controlli

```python tab="metric.py"
import json


def score(input, output, expected=None):
    try:
        data = json.loads(output)
    except ValueError as exc:
        return 0.0, {"feedback": f"Output is not valid JSON: {exc}", "scores": {"format": 0.0, "fields": 0.0}}
    want = expected["fields"]
    found = [key for key in want if key in data]
    fields = len(found) / len(want) if want else 1.0
    missing = [key for key in want if key not in data]
    feedback = "All fields present." if not missing else f"Missing fields: {', '.join(missing)}."
    return 0.5 + 0.5 * fields, {"feedback": feedback, "scores": {"format": 1.0, "fields": fields}}
```

### Modello come giudice

La metrica può chiamare un modello. Importa `llm` dal modulo di supporto `skynet`. Una chiamata dalla metrica va al modello della metrica.

```python tab="metric.py"
from skynet import llm


def score(input, output, expected=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {input['question']}\nAnswer: {output}",
        model="openai/gpt-4.1-mini",
        temperature=0,
        max_tokens=512,
        reasoning_effort="low",
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

La metrica imposta il proprio modello nella sua chiamata a `llm()`. Tutti e quattro gli argomenti mostrati sono facoltativi. Senza `model=`, la metrica usa il modello di riflessione.

## Accuratezza, velocità e costo

Una metrica può combinare l'accuratezza con il tempo di esecuzione misurato e il costo del modello. Definisci `REWARD_SPEC` a livello di modulo e restituisci il valore di `reward`.

```python tab="metric.py"
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.7, "speed": 0.2, "cost": 0.1},
    "accuracy": {},
    "speed": {"reference": 2000},
    "cost": {"reference": 0.01},
}


def score(input, output, expected=None):
    ok = output == expected["label"]
    return reward(REWARD_SPEC, 1.0 if ok else 0.0, "Correct." if ok else "Wrong label.")
```

- `weights` deve contenere `accuracy`, `speed` e `cost`. Ogni peso è 0 o maggiore, e non possono essere tutti 0. Skynet li normalizza in modo che la somma sia 1.
- `speed.reference` è in millisecondi. `cost.reference` è in dollari USA. Entrambi devono essere positivi.
- Senza `accuracy.reference`, l'accuratezza è un punteggio da 0 a 1. Con esso, l'accuratezza è un errore, e un valore più basso è migliore.

Skynet misura direttamente il tempo e il costo. Il codice di esecuzione non può modificarli. Se Skynet non riesce a misurare il tempo o il costo, quella parte ottiene 0 e il feedback lo segnala.

La metrica può anche leggere direttamente le misurazioni con `observation()` e `service_cost()` di `skynet`.

## Limiti

| Elemento                              | Valore                                      |
| ------------------------------------- | ------------------------------------------- |
| Timeout della metrica                 | 60 secondi per impostazione predefinita, al massimo 600 |
| Campioni del controllo del contratto  | 3 casi                                      |

## Pagine correlate

- [Dataset e suddivisioni](/docs/concepts/datasets)
- [Motori](/docs/concepts/engines)
- [Avviare un'esecuzione](/docs/guides/submit)
