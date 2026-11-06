Questa pagina mostra come avviare la tua prima esecuzione in circa dieci minuti. Accedi, carichi un piccolo dataset, compili i quattro passaggi della procedura guidata e avvii l'esecuzione. Nell'esempio, Skynet crea un nuovo repository che classifica i messaggi di assistenza in etichette, e poi lo migliora.

## Prima di iniziare

Ti servono:

- Un account Skynet.
- Un account GitHub. Skynet crea il repository in questo account.
- Un file CSV, JSON o Excel (`.csv`, `.json`, `.xlsx` o `.xls`). Ogni riga è un caso.
- Una o più colonne di input, ad esempio `message`.
- Una colonna di output atteso, ad esempio `label`.

> [!TIP]
> Usa almeno 5 righe. Con meno di 5 coppie, Skynet non tiene un set di test, quindi il punteggio finale proviene da casi che l'ottimizzatore ha usato.

## 1. Accesso

Accedi a Skynet. Collega GitHub in **Impostazioni → Connettori**. Poi apri la pagina di invio all'indirizzo `/submit`.

> [!NOTE]
> Questa pagina usa le etichette del livello **Standard**. Il livello **Guidato** usa etichette più semplici, salta il passaggio Ottimizzatore e seleziona **Auto** al posto tuo. Puoi cambiare il livello nelle Impostazioni.

## 2. Attività

Il primo passaggio è **Attività**. Chiede «Cosa vuoi ottimizzare?»

1. In **Obiettivo**, scrivi che cos'è un buon risultato. Usa parole semplici, ad esempio «Classifica ogni messaggio di assistenza come billing, bug o other.»
2. Facoltativo: in **Contesto**, aggiungi informazioni utili all'ottimizzatore.
3. Seleziona **Crea un nuovo repository**. Inserisci un nome, ad esempio `support-labels`, e seleziona **Privato** o **Pubblico**.

Skynet crea il repository nel tuo account GitHub. L'assistente scrive una prima soluzione a partire dalla tua attività e dai tuoi dati. Questa soluzione è la versione iniziale. Se hai già del codice, seleziona invece **Usa un repository**. Leggi [Avviare un'esecuzione](/docs/guides/submit).

## 3. Input

Il secondo passaggio è **Input**.

1. Seleziona **Carica un file CSV, JSON o Excel**, oppure **Scegli dalla libreria** per usare un dataset salvato.
2. In **Come leggere i dati**, assegna un ruolo a ogni colonna:
   - **Input**: il tuo codice riceve questa colonna.
   - **Output**: il valore atteso. Lo scorer lo riceve.
   - **Salta**: Skynet non usa questa colonna.
3. Mantieni **Usa il consiglio** per la suddivisione. Per la maggior parte dei dataset la suddivisione è 70% addestramento, 15% convalida e 15% test.

Leggi [Dataset e suddivisioni](/docs/concepts/datasets) per tutte le regole.

## 4. Ottimizzatore

Il terzo passaggio è **Ottimizzatore** («Ottimizzatore, modelli e budget»).

- Lascia l'ottimizzatore su **Auto**.
- Seleziona un **Modello di riflessione**. L'ottimizzatore lo usa per scrivere nuove versioni. Questo modello è obbligatorio.
- In **Budget**, lascia **Esecuzioni dello scorer max** al valore predefinito di 200.

Leggi [Motori](/docs/concepts/engines) per scegliere un motore specifico.

## 5. Metrica ed esecuzione

L'ultimo passaggio è **Metrica ed esecuzione**. L'assistente scrive lo scorer per te. Puoi modificarlo. Lo scorer riceve il percorso di una versione del repository e un caso:

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = str(module.serve(case["input"])).strip().lower()
    want = case["expected"]["label"].strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`case["input"]` contiene le colonne di input di un caso. `case["expected"]` contiene la colonna di output, con il nome della colonna come chiave. Il repository ha un file `serve.py` con `def serve(input)`. Skynet usa la stessa funzione per servire la versione migliore dopo l'esecuzione.

Poi esegui il controllo. Il controllo esegue la tua configurazione e il tuo scorer sulla versione iniziale nella sandbox. Puoi avviare l'esecuzione solo dopo che il controllo è superato.

## 6. Avvio e monitoraggio

La schermata di riepilogo mostra «Pronto per l'avvio». Le sue schede sono Generale, Dati, Modelli, Budget e Ricompensa. Controlla la configurazione, poi avvia l'ottimizzazione.

Apri l'esecuzione dall'elenco delle tue ottimizzazioni per seguirla. Skynet invia ogni candidato a un proprio branch nel tuo nuovo repository. Il branch `skynet/best` punta sempre alla versione migliore. Leggi [Risultati](/docs/guides/results) per capire i punteggi e la versione vincente.

## Passaggi successivi

- Scrivi uno scorer migliore: [Metriche e scorer](/docs/concepts/scorers).
- Scopri come funzionano i motori: [Come funziona l'ottimizzazione](/docs/concepts/optimization).
- Scopri quanto costa un'esecuzione: [Fatturazione](/docs/concepts/billing).
