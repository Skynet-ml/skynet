Il pannello dell'agente è un assistente in chat all'interno di Skynet. Può leggere le tue esecuzioni, i tuoi dataset e il tuo saldo, e può agire per tuo conto, ad esempio rinominare o annullare un'esecuzione. Sei tu a decidere quali azioni richiedono la tua approvazione.

## Aprire il pannello

Il pannello si apre a lato dell'app. Quando lo riduci a icona, resta sullo schermo una pillola con l'etichetta **Apri l'agente**. Fai clic sulla pillola per riaprire il pannello.

Fai clic su **Cosa posso fare** per vedere un elenco degli strumenti dell'agente.

### Conversazioni

- **Nuova conversazione** avvia una chat vuota.
- **Conversazioni passate** elenca le tue chat precedenti. Puoi cercarle, fissarle, rinominarle ed eliminarle.

### Inviare un messaggio

Scrivi il messaggio e premi Invio. Puoi anche fare clic su **Detta** e pronunciare il messaggio.

Mentre l’agente lavora, il campo del messaggio resta disponibile:

- **Invio** indirizza il lavoro corrente, dove supportato. L’agente legge il messaggio al passo successivo. **Tab** accoda un nuovo turno. Se non è possibile indirizzare, anche Invio accoda il messaggio.
- In **Messaggi in attesa** puoi modificare, eliminare e trascinare i messaggi per riordinarli. **Indirizza**, quando disponibile, passa un messaggio dalla coda al lavoro corrente.
- Un messaggio con l’etichetta **Non confermato** non è stato ritirato con certezza. Invialo ora, modificalo o eliminalo esplicitamente.
- **Interrompi** ferma il turno e mette in pausa la coda. Le istruzioni non lette tornano nell’elenco. Nulla parte automaticamente. **Esc** nel campo fa lo stesso, salvo con un menu aperto o durante la composizione con IME. Esc chiude prima un suggerimento o un menu di questo campo.
- Senza un turno attivo, **Invia ora** invia il primo messaggio della coda in pausa. Gli altri proseguono in ordine. Anche la conclusione normale di un turno avvia il messaggio successivo.
- **Interrompi e invia ora** ferma il turno e invia l’intero elenco come un unico nuovo turno. Attende il ritorno delle istruzioni non lette, poi le unisce prima dei messaggi in coda, separandoli con righe vuote. Non reinvia istruzioni già lette. I messaggi non confermati restano nell’elenco e non vengono inclusi. Gli altri continuano normalmente.
- L’assistente della documentazione e la chat protetta dei programmi distribuiti usano **solo la coda**. Invio e Tab accodano i messaggi. L’assistente della documentazione mette in pausa quelli restanti dopo un limite di richieste.

## Che cosa può fare l'agente

L'agente può leggere:

- Le tue esecuzioni, i loro log e i risultati dei test.
- Le analisi e il saldo del tuo portafoglio.
- I dataset della tua libreria e i risultati della ricerca pubblica.
- Le tue sessioni di etichettatura (in sola lettura).
- I dataset di esempio, il catalogo dei motori e il catalogo degli ottimizzatori.

L'agente può anche agire. Le azioni sicure includono:

- Rinominare e fissare le esecuzioni, e fissare molte esecuzioni in una volta.
- Mettere in pausa un'esecuzione.
- Modificare e convalidare il codice di un'esecuzione.
- Profilare un dataset e impostarne i ruoli delle colonne.
- Trovare i modelli disponibili.
- Aggiornare la procedura guidata di invio e le tue preferenze.
- Preparare un dataset di esempio.
- Creare una cartella.

Le azioni distruttive includono:

- Eliminare una o più esecuzioni.
- Inviare, annullare, clonare, riprovare, riprendere o riavviare un'esecuzione, e annullare molte esecuzioni.
- Eliminare, spostare o aggiornare una cartella, e spostare le esecuzioni tra cartelle.

> [!NOTE]
> Gli strumenti web (ricerca, recupero di un URL, scansione di un sito) sono disponibili solo quando il server ha configurato una chiave di ricerca web. Se non li vedi, la tua istanza di Skynet non li offre.

## Modalità di fiducia

La modalità di fiducia stabilisce quali azioni richiedono la tua approvazione. Fai clic sul selettore di fiducia nel compositore per cambiarla. Il selettore passa ciclicamente tra le tre modalità.

| Modalità                | Descrizione                                         | Azioni sicure            | Azioni distruttive       |
| ----------------------- | --------------------------------------------------- | ------------------------ | ------------------------ |
| **Chiedi**              | Chiede conferma prima di ogni azione.               | Richiedono approvazione  | Richiedono approvazione  |
| **Automatico**          | Esegue le azioni sicure senza conferma.             | Eseguite subito          | Richiedono approvazione  |
| **Senza restrizioni**   | Esegue tutto senza conferma.                        | Eseguite subito          | Eseguite subito          |

La modalità predefinita è **Chiedi**. Gli strumenti di sola lettura non richiedono mai approvazione.

> [!WARNING]
> In modalità **Senza restrizioni**, l'agente può eliminare esecuzioni o inviare esecuzioni a pagamento senza approvazione. Usa questa modalità solo quando ti fidi del compito.

### Impostare la modalità predefinita

1. Apri **Impostazioni**.
2. Apri la scheda **Pannello dell'agente**.
3. In **Livello di fiducia dell'agente**, scegli **Chiedi prima**, **Automatico solo per le azioni sicure** o **Nessuna approvazione preventiva**.

### Approvare un'azione

Quando un'azione richiede approvazione, il pannello mostra una scheda di approvazione. La scheda ha un pulsante di conferma per l'azione e **Annulla**. Fai clic su **Mostra i dettagli tecnici** per vedere la chiamata esatta allo strumento.

Se non rispondi entro 15 minuti, l'agente considera l'azione rifiutata.

## Allegare un file

Puoi allegare file a un messaggio. Ogni file può arrivare a 20 MB. Puoi allegare un dataset alla volta.

Per caricare un dataset tramite l'agente:

1. Allega un file CSV, JSON o Excel.
2. Controlla la scheda di caricamento. Mostra le colonne del file.
3. Imposta i ruoli delle colonne nella scheda, poi conferma.

Se l'agente non riesce a leggere il file, la scheda mostra «Impossibile leggere il file. Usa un file CSV, JSON o XLSX.»

Per le regole sui file, vedi [Preparare un dataset](/docs/guides/datasets).

## Scegliere un modello

Il menu dei modelli nel compositore ha questi gruppi:

- **Auto** lascia che Skynet scelga il modello.
- **Skynet credits** usa un modello ospitato, pagato dal tuo saldo.
- **Usa la tua chiave** usa un modello tramite la chiave del tuo provider.

Alcuni modelli ti permettono di impostare l'impegno di ragionamento. I livelli sono **Nessuno**, **Minimo**, **Basso**, **Medio**, **Alto**, **Molto alto** e **Massimo**. **Predefinito** usa il livello predefinito del modello.

Per impostare un modello predefinito per le nuove chat, apri **Impostazioni** > **Pannello dell'agente** > **Modello predefinito**.

### Costo

Le chat con l'agente sui crediti Skynet usano il tuo saldo. Se il saldo è esaurito, il pannello mostra «Il tuo saldo è esaurito. Aggiungi fondi per continuare a chattare con l'agente.» Per i prezzi, leggi [Budget e fatturazione](/docs/concepts/billing).

## Suggerimenti

> [!TIP]
> Chiedi all'agente di spiegarti un'esecuzione prima di modificarla. Ad esempio: «Perché l'esecuzione 42 ha ottenuto un punteggio più basso dell'esecuzione 41?»

> [!TIP]
> Usa la modalità **Chiedi** quando provi un nuovo tipo di richiesta. Passa ad **Automatico** quando sai che cosa farà l'agente.

## Altri modi di usare un agente

Puoi anche collegare il tuo client IA a Skynet tramite il server MCP. Vedi [Server MCP](/docs/mcp). Per gli script, usa l'[API](/docs/api).

## Passaggi successivi

- [Avvia un'esecuzione](/docs/guides/submit).
- [Leggi i risultati](/docs/guides/results).
- Leggi [Sicurezza](/docs/security) per sapere come Skynet protegge le tue chiavi.
