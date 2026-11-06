Questa pagina mostra come avviare la tua prima esecuzione in circa dieci minuti. Accedi, carichi un piccolo dataset, compili i quattro passaggi della procedura guidata e avvii l'esecuzione. L'esempio migliora un programma DSPy che classifica i messaggi di assistenza in etichette.

## Prima di iniziare

Ti servono:

- Un account Skynet.
- Un file CSV, JSON o Excel (`.csv`, `.json`, `.xlsx` o `.xls`). Ogni riga è un caso.
- Una o più colonne di input, ad esempio `message`.
- Una colonna di output atteso, ad esempio `label`.

> [!TIP]
> Usa almeno 5 righe. Con meno di 5 coppie, Skynet non tiene un set di test, quindi il punteggio finale proviene da casi che l'ottimizzatore ha usato.

## 1. Accesso

Accedi a Skynet. Poi apri la pagina di invio all'indirizzo `/submit`.

> [!NOTE]
> Questa pagina usa le etichette del livello **Standard**. Il livello **Guidato** usa etichette più semplici, salta il passaggio Ottimizzatore e seleziona **Auto** al posto tuo. Puoi cambiare il livello nelle Impostazioni.

## 2. Attività

Il primo passaggio è **Attività**. Chiede «Cosa vuoi ottimizzare?»

1. In **Obiettivo**, scrivi che cos'è un buon risultato. Usa parole semplici, ad esempio «Classifica ogni messaggio di assistenza come billing, bug o other.»
2. Facoltativo: in **Contesto**, aggiungi informazioni utili all'ottimizzatore.
3. In **Cosa viene ottimizzato**, seleziona **Programma DSPy**.
4. In **Versione iniziale**, incolla l'istruzione attuale del tuo programma.

## 3. Input

Il secondo passaggio è **Input**.

1. Seleziona **Carica un file CSV, JSON o Excel**, oppure **Scegli dalla libreria** per usare un dataset salvato.
2. In **Come leggere i dati**, assegna un ruolo a ogni colonna:
   - **Input**: il codice di esecuzione riceve questa colonna.
   - **Output**: il valore atteso. La metrica lo riceve.
   - **Salta**: Skynet non usa questa colonna.
3. Mantieni **Usa il consiglio** per la suddivisione. Per la maggior parte dei dataset la suddivisione è 70% addestramento, 15% convalida e 15% test.

Leggi [Dataset e suddivisioni](/docs/concepts/datasets) per tutte le regole.

## 4. Ottimizzatore

Il terzo passaggio è **Ottimizzatore** («Ottimizzatore, modelli e budget»).

- Lascia l'ottimizzatore su **Auto**.
- Seleziona un **Modello di riflessione**. L'ottimizzatore lo usa per scrivere nuove versioni. Questo modello è obbligatorio.
- Seleziona un **Modello dell'attività**. Il tuo programma lo usa. Un programma DSPy deve averne uno.
- In **Budget**, lascia **Esecuzioni dello scorer max** al valore predefinito di 200.

Leggi [Motori](/docs/concepts/engines) per scegliere un motore specifico.

## 5. Metrica ed esecuzione

L'ultimo passaggio è **Metrica ed esecuzione** («Codice e metrica»). Scrivi due file.

```python tab="run.py"
import dspy

program = dspy.Predict("message -> label")


def run(artifact, input):
    return program(message=input["message"]).label
```

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"].strip().lower()
    got = str(output).strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`artifact` è l'istruzione attuale proposta dall'ottimizzatore. Skynet la applica a `program`. `input` contiene le colonne Input di un caso. `expected` contiene la colonna Output, con il nome della colonna come chiave.

Poi seleziona **Controlla** in **Controllo del contratto**. Il controllo esegue il tuo codice e la tua metrica nella sandbox su 3 casi di esempio. Verifica:

| Controllo                            | Che cosa non lo supera                                                 |
| ------------------------------------ | ---------------------------------------------------------------------- |
| Firma del codice di esecuzione       | `run.py` non definisce `run(artifact, input)`                          |
| Firma della metrica                  | `metric.py` non definisce `score(input, output, expected=None)`        |
| Modello della metrica                | Il modello della metrica non è utilizzabile                            |
| Codice di esecuzione sugli esempi    | Il codice di esecuzione genera un errore su un caso di esempio         |
| Valore restituito dalla metrica      | `score` non restituisce un punteggio e un feedback validi              |

> [!NOTE]
> Puoi avviare l'esecuzione solo dopo che il controllo del contratto è superato.

## 6. Avvio e monitoraggio

La schermata di riepilogo mostra «Pronto per l'avvio». Le sue schede sono Generale, Dati, Modelli, Budget e Ricompensa. Controlla la configurazione, poi avvia l'ottimizzazione.

Apri l'esecuzione dall'elenco delle tue ottimizzazioni per seguirla. Leggi [Risultati](/docs/guides/results) per capire i punteggi e la versione vincente.

## Passaggi successivi

- Scrivi una metrica migliore: [Metriche e scorer](/docs/concepts/scorers).
- Scopri quali motori si adattano al tuo artefatto: [Come funziona l'ottimizzazione](/docs/concepts/optimization).
- Scopri quanto costa un'esecuzione: [Fatturazione](/docs/concepts/billing).
