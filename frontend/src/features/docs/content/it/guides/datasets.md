Un dataset è una tabella di esempi da cui un'esecuzione di ottimizzazione impara e su cui assegna i punteggi. Questa guida mostra come caricare un file, modificare righe e colonne, impostare i ruoli delle colonne e condividere un dataset. Per i concetti, leggi [Dataset](/docs/concepts/datasets).

## File supportati

Skynet legge questi tipi di file:

| Estensione      | Come lo legge Skynet                                                                          |
| --------------- | --------------------------------------------------------------------------------------------- |
| `.csv`          | La prima riga è l'intestazione. Il file deve avere almeno una riga di dati.                   |
| `.json`         | Un array di oggetti, oppure un singolo oggetto. Le chiavi del primo oggetto diventano le colonne. |
| `.xlsx`, `.xls` | Skynet legge solo il primo foglio.                                                            |

Skynet rifiuta le altre estensioni con il messaggio «Unsupported file format».

> [!TIP]
> Metti l'output atteso nell'ultima colonna. La procedura guidata di invio contrassegna l'ultima colonna come output per impostazione predefinita.

### Esempio CSV

```csv
question,answer
What is the capital of France?,Paris
How many legs does a spider have?,8
```

### Esempio JSON

```json
[
  { "question": "What is the capital of France?", "answer": "Paris" },
  { "question": "How many legs does a spider have?", "answer": "8" }
]
```

## Caricare un dataset

1. Apri **Dataset**.
2. Fai clic su **Carica dataset**, oppure trascina un file sulla pagina. Durante il trascinamento la pagina mostra «Rilascia per caricare».
3. Attendi la notifica «Dataset salvato nella tua libreria».

Skynet usa il nome del file senza estensione come nome del dataset. Per cambiarlo, seleziona il dataset e fai clic su **Rinomina**.

> [!NOTE]
> Ogni account ha una quota di archiviazione. Se un caricamento supera la quota, Skynet apre una finestra che spiega il limite. Vedi [Limiti](/docs/limits).

Puoi anche caricare un file nella procedura guidata di invio («Carica un file CSV, JSON o Excel») oppure allegarne uno nel [pannello dell'agente](/docs/guides/agent-panel).

## Trovare un dataset

La libreria elenca i tuoi dataset. Usa **Cerca per nome** per filtrare l'elenco. I dataset che altre persone condividono con te compaiono in **Condiviso con me**.

Quando selezioni un dataset, compaiono queste azioni:

- **Etichetta questo dataset** apre il [tagger](/docs/guides/tagger) con questo dataset.
- **Modifica dati** apre l'editor del dataset.
- **Condividi** apre la finestra di condivisione.
- **Rinomina** cambia il nome del dataset.

Le altre azioni sono **Salva una copia** ed **Elimina**.

> [!WARNING]
> Elimina rimuove definitivamente il dataset dalla tua libreria. Non puoi annullare l'operazione. Le esecuzioni create dal dataset restano valide, ma il loro collegamento al dataset si interrompe.

## Modificare un dataset

L'editor mostra il dataset come una griglia. Mostra 100 righe per pagina. Usa **Righe successive** e **Righe precedenti** per spostarti tra le pagine.

### Modificare righe e colonne

- **Aggiungi riga** e **Aggiungi colonna** aggiungono nuove celle alla fine.
- **Inserisci riga sopra** e **Inserisci riga sotto** aggiungono una riga accanto alla riga selezionata.
- **Duplica riga**, **Elimina riga**, **Elimina colonna** e **Rinomina colonna** modificano la struttura.
- **Trascina per compilare** copia il valore di una cella nelle celle adiacenti.
- **Annulla** e **Ripeti** annullano o ripetono l'ultima modifica.

Fai doppio clic su una riga per vederla per intero.

### Salvataggio

L'editor salva automaticamente le modifiche, poco dopo ogni intervento. Durante il salvataggio lo stato mostra **Salvataggio…**. Se il salvataggio non riesce, lo stato mostra **Salvataggio non riuscito**.

> [!NOTE]
> L'editor non può salvare un dataset senza righe. Mostra «Aggiungi una riga per salvare le modifiche» finché non ne aggiungi una.

Fai clic su **Torna alla libreria** quando hai finito.

### Accesso in sola lettura

Se hai il ruolo Visualizzatore su un dataset condiviso, l'editor si apre in modalità di sola lettura. Per modificare i dati, chiedi al proprietario il ruolo Editor, oppure usa **Salva una copia**.

### Etichettare con l'IA

L'editor ha l'azione **Etichetta con l'IA**. Apre il tagger, dove un modello IA ti aiuta ad aggiungere etichette. Vedi [Etichettare i dati con il tagger](/docs/guides/tagger).

## Impostare i ruoli delle colonne

Un'esecuzione deve sapere quali colonne sono input e quale colonna è l'output atteso. Imposti questi ruoli quando invii un'esecuzione, non nell'editor del dataset.

Nella procedura guidata di invio, il passaggio «Come leggere i dati» mostra ogni colonna con tre scelte:

- **Input**: il tuo scorer riceve questo valore in `case["input"]`.
- **Output**: la risposta attesa con cui lo scorer confronta l'output.
- **Salta**: l'esecuzione ignora questa colonna.

I valori predefiniti sono:

- Ogni colonna è un input, e l'ultima colonna è l'output.
- Un dataset con una sola colonna ha un input e nessun output.

Senza una colonna di output, Skynet usa la prima riga come unico input. Devi scegliere almeno una colonna di input.

Alcune colonne possono anche contenere immagini. Per queste colonne, scegli **Immagine** o **Testo** per indicare a Skynet come leggere i valori.

> [!TIP]
> L'editor del dataset mostra un badge «output» sulle colonne di output. Una nuova colonna aggiunta nell'editor parte come input.

La procedura guidata suddivide anche le righe in parti per l'addestramento e la valutazione. La somma delle parti deve essere 1. Se non lo è, la procedura guidata mostra «La suddivisione deve sommare a 1.». Per il flusso completo, leggi [Avviare un'esecuzione](/docs/guides/submit).

## Condividere un dataset

Seleziona un dataset e fai clic su **Condividi**. Si apre la finestra **Condivisione e privacy**.

### Accesso tramite link

| Opzione                        | Chi può aprire il dataset                                                     |
| ------------------------------ | ----------------------------------------------------------------------------- |
| **Limitato**                   | Solo il proprietario e le persone invitate.                                   |
| **Chiunque abbia il link**     | Qualsiasi persona che ha effettuato l'accesso e ha il link, con il ruolo che scegli. |

Per un dataset «Chiunque abbia il link», scegli il ruolo che il link concede: **Visualizzatore** o **Editor**. Fai clic su **Copia il link** per copiare l'URL.

### Invitare persone

1. In **Invita persone**, digita un nome utente in **Aggiungi persone tramite nome utente**.
2. Fai clic su **Invita**.
3. Controlla l'elenco in **Persone con accesso**. Usa **Cambia ruolo** per cambiare il ruolo di una persona.

I ruoli sono:

| Ruolo           | Accesso                                                                       |
| --------------- | ----------------------------------------------------------------------------- |
| Proprietario    | Controllo completo, inclusa la gestione della condivisione e l'eliminazione.  |
| Editor          | Accesso da visualizzatore più esecuzione del modello, chat e rinomina.        |
| Visualizzatore  | Apre il dataset. L'editor è in sola lettura.                                  |

### Trasferire la proprietà

Usa **Trasferisci la proprietà** per cedere il dataset a un'altra persona. Skynet ti chiede di confermare.

> [!WARNING]
> Dopo il trasferimento diventi editor e perdi i privilegi di proprietario.

## Passaggi successivi

- [Avvia un'esecuzione](/docs/guides/submit) con il tuo dataset.
- [Etichetta i dati con il tagger](/docs/guides/tagger) se le tue righe non hanno ancora etichette.
- Leggi la pagina sugli [scorer](/docs/concepts/scorers) per scegliere come Skynet confronta gli output.
