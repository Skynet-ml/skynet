La procedura guidata di invio configura e avvia un'esecuzione. Ha quattro passaggi: **Attività**, **Input**, **Ottimizzatore** e **Metrica ed esecuzione**. Ogni passaggio ha una o più schermate. Questa guida spiega ogni schermata, dall'attività fino al controllo del contratto e all'avvio dell'esecuzione.

> [!NOTE]
> La procedura guidata mostra più o meno impostazioni a seconda del livello di esperienza: **Guidato**, **Standard** o **Esperto**. Guidato è il livello predefinito. Cambia il livello in **Impostazioni → Account**, in **Quanto mostrare**. Al livello Guidato alcune etichette sono più semplici. Questa guida usa le etichette dei livelli Standard ed Esperto e indica tra parentesi l'etichetta del livello Guidato quando è diversa.

## Passaggio 1: Attività

### Cosa vuoi ottimizzare?

Descrivi la tua attività all'assistente nella chat **Intervista sull'attività**. L'assistente compila due campi per te:

- **Obiettivo** (Guidato: **Scopo**): che cosa deve fare bene l'artefatto ottimizzato.
- **Contesto** (Guidato: **Contesto aggiuntivo**): contesto, vincoli e com'è un buon risultato.

Puoi modificare entrambi i campi. L'assistente non sovrascrive le tue modifiche.

### Che cosa verrà ottimizzato

Seleziona il **Tipo di artefatto**:

| Tipo di artefatto | Che cosa cambia                                              |
| ----------------- | ------------------------------------------------------------ |
| Prompt            | Le istruzioni inviate a un modello linguistico               |
| Codice            | Il sorgente di un programma                                  |
| Programma DSPy    | Le istruzioni dei predictor di un modulo DSPy                |
| Testo             | Qualsiasi altro testo, come un documento o un template       |

Poi fornisci la **Versione iniziale** (Guidato: **Da dove partiamo**). Incolla la versione attuale, oppure lascia che l'assistente scriva una bozza.

### Ottimizzare un repository

Puoi anche ottimizzare il codice di un repository. Imposta questi campi:

- **Repository**: scegli un repository, oppure scrivilo come `owner/name`.
- **Commit**: uno SHA di 40 caratteri. Lascialo vuoto per usare l'ultimo commit del branch.
- **Percorsi che l'ottimizzatore può modificare**: un file o una cartella per riga. Un punto indica l'intero repository.
- **Comando di setup**: viene eseguito in ogni nuovo checkout prima della valutazione. Lascialo vuoto e Skynet troverà il comando.

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

- **Modello dell'attività**: il modello chiamato dal tuo codice di esecuzione. Lascialo vuoto se il codice di esecuzione non chiama un modello. Un programma DSPy deve avere un modello dell'attività.
- **Modello di riflessione**: il modello che legge i punteggi e il feedback e scrive i candidati successivi. Con un ottimizzatore basato su un agente di programmazione, è il modello dell'agente.

### Budget

Imposta per quanto tempo l'esecuzione può continuare a provare e quanto può spendere:

- **Esecuzioni dello scorer max** (Guidato: **Tentativi max**): il numero massimo di candidati a cui Skynet assegna un punteggio. Alcuni ottimizzatori richiedono un numero minimo.
- **Fermati al punteggio** (Guidato: **Fermati prima quando il punteggio raggiunge**): l'esecuzione si ferma quando una versione raggiunge questo punteggio.
- **Iterazioni max** (Guidato: **Round max**): mostrato per gli ottimizzatori che lo supportano.
- **Limite di spesa**: il massimo che l'esecuzione può spendere. Il valore predefinito è 5 $, oppure l'importo **Necessario per iniziare** se è maggiore. Puoi anche selezionare **Nessun limite**.

La procedura guidata non ti fa proseguire se il limite o il tuo saldo è inferiore all'importo necessario per iniziare. Leggi [Budget e fatturazione](/docs/concepts/billing).

## Passaggio 4: Metrica ed esecuzione

### Codice e metrica

Scrivi due file:

- `run.py`: il codice di esecuzione che esegue il tuo artefatto su un caso.
- `metric.py`: la metrica che assegna un punteggio all'output e scrive il feedback.

L'**Agente di programmazione** può scrivere e modificare qualsiasi file per te. Controlla il proprio codice e corregge ciò che non funziona. Puoi aggiungere fino a 20 file aggiuntivi. Leggi [Metriche e scorer](/docs/concepts/scorers).

La metrica imposta il proprio modello nella sua chiamata a `llm()`. Senza `model=`, la metrica usa il modello di riflessione.

### Controllo del contratto

Il **Controllo del contratto** (Guidato: **Esecuzione di prova**) esegue il tuo codice e la tua metrica nella sandbox su 3 casi di esempio. Verifica:

- la firma del codice di esecuzione,
- la firma della metrica,
- il modello della metrica,
- il codice di esecuzione sui campioni,
- il valore restituito dalla metrica.

Seleziona **Controlla** (Guidato: **Provalo**). Non puoi avviare l'esecuzione finché il controllo non è superato. Se modifichi il codice o la configurazione dopo un controllo superato, esegui di nuovo il controllo.

> [!TIP]
> Apri ogni campione per vederne input, output e feedback. Un buon feedback dà all'ottimizzatore molte più informazioni del solo punteggio.

### Esecuzione pianificata

Puoi eseguire di nuovo l'ottimizzazione secondo una pianificazione dopo che è riuscita. Il valore predefinito è **Disattivata**.

### Pronto per l'avvio

L'ultima schermata mostra un riepilogo in cinque schede: **Generale**, **Dati**, **Modelli**, **Budget** e **Ricompensa**. Controlla la configurazione, poi avvia l'ottimizzazione.

Il riepilogo mostra anche la visibilità dell'esecuzione. Le nuove esecuzioni sono pubbliche per impostazione predefinita. Puoi cambiarla in seguito nella finestra **Condivisione e privacy** della pagina dell'esecuzione. Leggi [Condividere e pubblicare](/docs/guides/sharing).

> [!NOTE]
> Un'esecuzione su repository ha in questo passaggio solo la schermata **Codice e metrica** e il riepilogo. Skynet controlla la configurazione rispetto al repository quando avvii l'esecuzione.

## Dopo l'avvio

Si apre la pagina dell'esecuzione. Leggi [Esecuzioni](/docs/concepts/runs) e [Leggere i risultati](/docs/guides/results).
