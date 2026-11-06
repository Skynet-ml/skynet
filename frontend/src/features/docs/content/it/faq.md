Questa pagina risponde alle domande più comuni su Skynet. Ogni risposta rimanda alla pagina con tutti i dettagli.

## Generale

### Che cos'è Skynet?

Skynet è una piattaforma che ottimizza programmi DSPy e repository con i tuoi dati e la tua metrica. Carichi un dataset, descrivi l'attività e avvii un'esecuzione. L'esecuzione prova nuove versioni e assegna loro un punteggio su dati tenuti da parte. Per iniziare, leggi l'[Avvio rapido](/docs/quickstart).

### Che cosa può ottimizzare Skynet?

Skynet è Optimize Anything. Ottimizza programmi DSPy e repository in un unico prodotto. Il tipo di artefatto decide quali motori possono lavorarci:

- I repository usano i motori nativi: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler e ShinkaEvolve.
- I programmi DSPy usano gli ottimizzatori DSPy: DSPy GEPA, DSPy MIPROv2 e DSPy BootstrapFewShot.

Su un programma DSPy, **Auto** usa DSPy GEPA. Su un repository, scegli un motore nativo. Leggi [Come funziona l'ottimizzazione](/docs/concepts/optimization) e [Motori](/docs/concepts/engines).

### Quali lingue supporta l'interfaccia?

L'interfaccia è disponibile in 24 lingue. L'ebraico è la lingua di base e il layout supporta il testo da destra a sinistra. La documentazione in ebraico è su [/docs/he](/docs/he).

### Skynet è open source?

Sì. Il codice usa la licenza AGPL-3.0. Se offri come servizio una versione modificata di Skynet, la licenza ti obbliga a condividere le tue modifiche.

## Dati

### Quali tipi di file posso caricare?

Puoi caricare file CSV, JSON, `.xlsx` e `.xls`. Per i file Excel, Skynet legge solo il primo foglio. Vedi [Preparare un dataset](/docs/guides/datasets).

### I miei dati non hanno etichette. Che cosa posso fare?

Usa il tagger. Puoi etichettare le righe da solo, oppure lasciare che un modello IA le etichetti mentre tu ne revisioni il lavoro. Vedi [Etichettare i dati con il tagger](/docs/guides/tagger).

### Chi può vedere le mie esecuzioni e i miei dataset?

> [!WARNING]
> Le nuove esecuzioni sono **Pubbliche** per impostazione predefinita. Un'esecuzione pubblica compare nella ricerca pubblica e chiunque può sfogliarne i risultati.

Imposta **Visibilità** su **Privata** quando invii un'esecuzione per tenerla fuori dalla ricerca pubblica. I nuovi dataset sono **Limitati**: solo tu e le persone che inviti potete aprirli. Vedi [Condivisione](/docs/guides/sharing) e [Sicurezza](/docs/security).

### I provider di modelli si addestrano sui miei prompt?

Per impostazione predefinita, no. L'opzione predefinita di **Privacy dei dati dei modelli** è **Nessun addestramento**. Invia i prompt solo ai provider che non li usano per l'addestramento e non li conservano. Questa impostazione si applica alle chiamate che Skynet addebita al tuo saldo. Le chiamate BYOK tramite OpenRouter seguono le impostazioni di privacy del tuo account OpenRouter. Le chiamate BYOK dirette seguono la politica sui dati del provider. Vedi [Sicurezza](/docs/security).

## Fatturazione

### Come addebita Skynet le esecuzioni?

Skynet usa un saldo prepagato. L'uso dei modelli gestiti e il relativo ricarico, insieme all'uso della sandbox, sono misurati. La ricarica del saldo prevede una commissione. Prima che inizi un lavoro a pagamento, Skynet riserva un margine nel tuo portafoglio e nel budget dell'esecuzione. Vedi [Fatturazione](/docs/concepts/billing).

### Posso usare la chiave del mio provider di modelli?

Sì. Con la tua chiave (BYOK), il provider addebita i token del modello sulla tua chiave. Skynet non applica costi per il modello. Il budget dell’esecuzione copre solo l’uso della sandbox. Skynet cifra la tua chiave e non la mostra mai più. Vedi [Fatturazione](/docs/concepts/billing) e [Sicurezza](/docs/security).

### Posso usare Skynet senza pagare le chiamate ai modelli?

L'[app per Mac](/docs/guides/mac-app) esegue Skynet sul tuo computer. Invia le chiamate ai modelli tramite Claude Code o Codex, con l'accesso effettuato sul tuo Mac. Il tuo piano della CLI paga quelle chiamate, quindi aggiungono 0 $ all'esecuzione.

## Strumenti e integrazioni

### Posso controllare Skynet dal mio client IA?

Sì. Skynet ha un server MCP all'indirizzo `https://mcp.skynetml.com/mcp`. Accedi con OAuth. Il server offre questi strumenti: `check_run`, `start_run`, `get_run`, `list_runs` e `stop_run`. Vedi [MCP](/docs/mcp).

### Esiste un'API?

Sì. Genera un token API nelle Impostazioni, poi invialo come `Authorization: Bearer <token>`. Ogni utente ha un solo token valido alla volta. Vedi [API](/docs/api).

### Il pannello dell'agente può eliminare le mie esecuzioni?

Solo se lo consenti. Nella modalità predefinita **Chiedi**, l'agente chiede conferma prima di ogni azione. Nella modalità **Automatico**, chiede comunque conferma prima delle azioni distruttive, come l'eliminazione o l'invio. Vedi [Pannello dell'agente](/docs/guides/agent-panel).
