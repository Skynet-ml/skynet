La procedura guidata di invio configura e avvia un'esecuzione. Ha quattro passaggi: **Attività**, **Input**, **Ottimizzatore** e **Metrica ed esecuzione**. Ogni passaggio ha una o più schermate. Questa guida spiega ogni schermata, dall'attività fino al controllo e all'avvio dell'esecuzione.

> [!NOTE]
> La procedura guidata mostra più o meno impostazioni a seconda del livello di esperienza: **Guidato**, **Standard** o **Esperto**. Guidato è il livello predefinito. Cambia il livello in **Impostazioni → Account**, in **Quanto mostrare**. Al livello Guidato alcune etichette sono più semplici. Questa guida usa le etichette dei livelli Standard ed Esperto e indica tra parentesi l'etichetta del livello Guidato quando è diversa.

## Passaggio 1: Attività

### Cosa vuoi ottimizzare?

Descrivi la tua attività all'assistente nella chat **Intervista sull'attività**. L'assistente compila due campi per te:

- **Obiettivo** (Guidato: **Scopo**): che cosa deve fare bene il codice ottimizzato.
- **Contesto** (Guidato: **Contesto aggiuntivo**): contesto, vincoli e com'è un buon risultato.

Puoi modificare entrambi i campi. L'assistente non sovrascrive le tue modifiche.

### Da dove viene il codice

Skynet è Optimize Anything: ogni esecuzione ottimizza un repository. Seleziona una delle due scelte:

- **Usa un repository**: ottimizza il codice di un repository GitHub che hai collegato.
- **Crea un nuovo repository**: Skynet crea un nuovo repository GitHub nel tuo account. L'assistente scrive una prima soluzione a partire dalla tua attività e dai tuoi dati. Questa soluzione è la versione iniziale.

Collega GitHub in **Impostazioni → Connettori** prima di iniziare.

### Usa un repository

Imposta questi campi:

- **Repository**: scegli un repository, oppure scrivilo come `owner/name`.
- **Commit**: uno SHA di 40 caratteri. Lascialo vuoto per usare l'ultimo commit del branch.
- **Percorsi che l'ottimizzatore può modificare**: un file o una cartella per riga. Un punto indica l'intero repository.
- **Comando di setup**: viene eseguito in ogni nuovo checkout prima della valutazione. Lascialo vuoto e Skynet troverà il comando.

### Crea un nuovo repository

Imposta questi campi:

- **Nome**: il nome del nuovo repository. Il tuo account GitHub non deve avere già un repository con questo nome.
- **Privato** o **Pubblico**: chi può vedere il repository su GitHub.

Skynet invia ogni candidato a un proprio branch. Il branch `skynet/best` punta alla versione migliore.

## Passaggio 2: Input

### Dati

Aggiungi i casi per l'esecuzione. Puoi:

- Caricare un file CSV, JSON o Excel.
- Scegliere un dataset dalla tua libreria.

Leggi [Dataset e suddivisioni](/docs/concepts/datasets) e [Preparare un dataset](/docs/guides/datasets).

### Come leggere i dati

L'assistente legge i dati e suggerisce come usarli. Controlla le sue scelte:

- Contrassegna ogni colonna come input, come output atteso, oppure ignorala.
- Senza una colonna di output, Skynet usa la prima riga come unico input.
- Imposta la suddivisione in set di addestramento, convalida e test. La somma della suddivisione deve essere 1.

> [!WARNING]
> Non puoi aprire i passaggi successivi finché il passaggio **Input** ha un problema. Correggi i problemi che la procedura guidata mostra, poi continua.

## Passaggio 3: Ottimizzatore

Al livello Standard, questo passaggio ha una sola schermata. Mostra l'ottimizzatore, il budget e, se necessario, i modelli. Ai livelli Guidato ed Esperto, il passaggio ha tre schermate: **Ottimizzatore**, **Modelli** e **Budget**.

### Ottimizzatore

La procedura guidata elenca solo gli ottimizzatori adatti alla tua attività e ai tuoi input. **Auto** è già selezionato. Se vuoi, seleziona un altro ottimizzatore. Leggi [Motori](/docs/concepts/engines) e [Come funziona l'ottimizzazione](/docs/concepts/optimization).

### Modelli

- **Modello di riflessione**: il modello che legge i punteggi e il feedback e scrive i candidati successivi. Con un ottimizzatore basato su un agente di programmazione, è il modello dell'agente.
- **Modello dell'attività**: il modello che il codice del tuo repository e il tuo scorer possono chiamare tramite il gateway dei modelli di Skynet. Leggi [Come funziona l'ottimizzazione](/docs/concepts/optimization).

### Budget

Imposta per quanto tempo l'esecuzione può continuare a provare e quanto può spendere:

- **Esecuzioni dello scorer max** (Guidato: **Tentativi max**): il numero massimo di candidati a cui Skynet assegna un punteggio. Alcuni ottimizzatori richiedono un numero minimo.
- **Fermati al punteggio** (Guidato: **Fermati prima quando il punteggio raggiunge**): l'esecuzione si ferma quando una versione raggiunge questo punteggio.
- **Iterazioni max** (Guidato: **Round max**): mostrato per gli ottimizzatori che lo supportano.
- **Limite di spesa**: il massimo che l'esecuzione può spendere. Il valore predefinito è 5 $, oppure l'importo **Necessario per iniziare** se è maggiore. Puoi anche selezionare **Nessun limite**.

La procedura guidata non ti fa proseguire se il limite o il tuo saldo è inferiore all'importo necessario per iniziare. Leggi [Budget e fatturazione](/docs/concepts/billing).

## Passaggio 4: Metrica ed esecuzione

### Scorer

Scrivi `scorer.py`. Definisce `score(repo_path, case)` e restituisce un punteggio con un feedback. L'assistente può scriverlo per te. Leggi [Metriche e scorer](/docs/concepts/scorers).

Il repository può avere un file `serve.py` con `def serve(input)`. Skynet lo usa per servire la versione migliore dopo l'esecuzione. Leggi [Leggere i risultati](/docs/guides/results).

### Secret

Aggiungi i secret, come le chiavi API di cui ha bisogno il tuo codice. Ogni segreto arriva al tuo codice e al tuo scorer come variabile d'ambiente con lo stesso nome. L'agente di programmazione non può vederli. Skynet nasconde i loro valori nei log.

### Controllo

Il controllo (Guidato: **Esecuzione di prova**) esegue il setup e il tuo scorer sulla versione iniziale nella sandbox. Verifica:

- la firma dello scorer,
- il comando di setup,
- il valore restituito dallo scorer sui casi di esempio.

Seleziona **Controlla** (Guidato: **Provalo**). Non puoi avviare l'esecuzione finché il controllo non è superato. Se modifichi lo scorer o la configurazione dopo un controllo superato, esegui di nuovo il controllo.

> [!TIP]
> Apri ogni campione per vederne il punteggio e il feedback. Un buon feedback dà all'ottimizzatore molte più informazioni del solo punteggio.

### Esecuzione pianificata

Puoi eseguire di nuovo l'ottimizzazione secondo una pianificazione dopo che è riuscita. Ogni ciclo pianificato parte dalla versione migliore attuale del repository. Il valore predefinito è **Disattivata**.

### Pronto per l'avvio

L'ultima schermata mostra un riepilogo in cinque schede: **Generale**, **Dati**, **Modelli**, **Budget** e **Ricompensa**. Controlla la configurazione, poi avvia l'ottimizzazione.

Il riepilogo mostra anche la visibilità dell'esecuzione. Le nuove esecuzioni sono pubbliche per impostazione predefinita. Puoi cambiarla in seguito nella finestra **Condivisione e privacy** della pagina dell'esecuzione. Leggi [Condividere e pubblicare](/docs/guides/sharing).

## Dopo l'avvio

Si apre la pagina dell'esecuzione. Leggi [Esecuzioni](/docs/concepts/runs) e [Leggere i risultati](/docs/guides/results).
