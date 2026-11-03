Questa pagina descrive come Skynet protegge il tuo account, le tue chiavi e i tuoi dati. Spiega anche come segnalare un problema di sicurezza.

## Segnalare una vulnerabilità

Segnala i problemi di sicurezza in privato. Non aprire una issue pubblica.

- Su GitHub, apri il repository di Skynet e vai a **Security** > **Report a vulnerability**.
- Oppure invia un'email a `security@skynetml.com`.

Il team si impegna a confermare la ricezione di ogni segnalazione entro cinque giorni lavorativi.

## Il tuo account

### Metodi di accesso

Puoi accedere con:

- Un indirizzo email e una password.
- **Continua con Google** o **Continua con GitHub**.
- **Accedi con una passkey**. Una passkey usa Face ID, Touch ID o una chiave hardware.

Puoi gestire i provider che usi in **Account collegati**.

### Regole per la password

Una password deve:

- Avere almeno 8 caratteri.
- Avere al massimo 128 caratteri.
- Non contenere il tuo indirizzo email.

Un nuovo account deve verificare il proprio indirizzo email con un codice. Anche la reimpostazione della password usa un codice che Skynet invia per email.

Dopo ripetuti tentativi falliti, Skynet blocca per un certo periodo l'accesso per quell'indirizzo email.

### Verifica in due passaggi

Attiva la verifica in due passaggi per una protezione maggiore. Scegli un metodo:

| Metodo                      | Come funziona                                                             |
| --------------------------- | ------------------------------------------------------------------------- |
| **App di autenticazione**   | Inserisci un codice di 6 cifre dalla tua app di autenticazione.           |
| **Codici via email**        | Inserisci un codice di 6 cifre che Skynet invia a ogni accesso.           |

Skynet ti fornisce anche dei codici di recupero. Conservali in un luogo sicuro. Usane uno se perdi l'accesso al tuo metodo.

> [!NOTE]
> Se accedi con Google o GitHub, è quel provider a gestire la verifica in due passaggi.

## Dove gira il tuo codice

I carichi di lavoro degli utenti girano in sandbox isolate. Nel prodotto ospitato, Vercel Sandbox è il confine di esecuzione. Questo vale per i controlli della configurazione, le esecuzioni di ottimizzazione, il ripristino e il lavoro sulle esecuzioni completate.

I worker di Skynet non eseguono il tuo codice. Sono il piano di controllo. Avviano i job, riservano e regolano i budget, forniscono credenziali con ambito limitato alle sandbox e salvano i checkpoint.

Prima di avviare un lavoro a pagamento, Skynet riserva un margine nel tuo portafoglio e nel budget dell'esecuzione. Vedi [Fatturazione](/docs/concepts/billing).

Nell'[app per Mac](/docs/guides/mac-app), i job girano in container Docker locali. Tutti i servizi locali ascoltano solo su `127.0.0.1`.

## Chiavi dei provider (BYOK)

Puoi aggiungere le chiavi dei tuoi provider di modelli.

- Skynet cifra ogni chiave a riposo.
- Skynet non restituisce mai la chiave completa tramite l'API.
- Skynet conserva solo gli ultimi quattro caratteri e uno stato: non verificata, verificata o non valida.
- Quando inserisci una chiave, Skynet invia una piccola richiesta di prova per controllarla.

Il provider addebita i token del modello sulla tua chiave. Skynet non applica costi per il modello. Il budget dell’esecuzione copre solo l’uso della sandbox.

Skynet rimuove anche i segreti da log e messaggi di errore. Li sostituisce con `[REDACTED]`.

## Token API

I token API permettono a script e strumenti di chiamare l'API di Skynet.

- Ogni utente ha al massimo un token valido. Quando generi un nuovo token, quello vecchio smette di funzionare.
- I token iniziano con `skyd_`.
- Skynet conserva solo un hash del token.
- Skynet mostra il token completo una sola volta. Copia e conserva subito il token.
- Dopo, Skynet mostra solo una forma mascherata: `skyd_…` e gli ultimi quattro caratteri.

Invia il token nell'intestazione `Authorization`:

```http
Authorization: Bearer skyd_...
```

Per disattivare un token, fai clic su **Revoca token**. Per l'API completa, leggi [API](/docs/api).

> [!WARNING]
> Tratta il token come una password. Chiunque abbia il token può agire a tuo nome tramite l'API. Non salvarlo in un repository.

## Privacy dei dati dei modelli

L'impostazione **Privacy dei dati dei modelli** controlla che cosa possono fare i provider di modelli con i tuoi dati. Si applica alle ottimizzazioni che Skynet addebita al tuo saldo.

| Opzione                      | Effetto                                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------------------- |
| **Consenti tutti**           | Qualsiasi provider, compresi quelli che possono addestrarsi sui tuoi prompt o conservarli.                |
| **Nessun addestramento**     | Solo provider che non si addestrano sui tuoi prompt e non li conservano. È l'opzione predefinita.         |
| **Nessuna conservazione**   | Solo endpoint con un accordo di conservazione zero dei dati. Alcuni modelli possono non essere disponibili. |

> [!NOTE]
> Le chiamate BYOK tramite OpenRouter seguono le impostazioni di privacy del tuo account OpenRouter. Le chiamate BYOK dirette seguono la politica sui dati del provider.

## Visibilità delle esecuzioni

> [!WARNING]
> Le nuove esecuzioni sono **Pubbliche** per impostazione predefinita. Un'esecuzione pubblica compare nella ricerca pubblica. Chiunque può trovarla e sfogliarne i risultati.

Per tenere un'esecuzione fuori dalla ricerca pubblica, imposta **Visibilità** su **Privata** quando la invii. Puoi cambiare la visibilità in seguito nella finestra di condivisione. Vedi [Condivisione](/docs/guides/sharing).

I nuovi dataset sono **Limitati**. Solo tu e le persone che inviti potete aprirli, finché non modifichi l'accesso tramite link. Vedi [Preparare un dataset](/docs/guides/datasets).

## I tuoi dati

Apri la scheda **Privacy e dati** in **Impostazioni** per gestire i tuoi dati.

### Esportare i dati

Fai clic su **Esporta** in **Esporta i miei dati**. L'esportazione contiene tutti i dati del tuo account. Scegli un formato: JSON, CSV, Excel, Pickle, Parquet o Feather.

### Eliminare l'account

Fai clic su **Elimina account**. Inserisci la password attuale e digita il tuo indirizzo email per confermare.

> [!WARNING]
> Questa azione elimina definitivamente il tuo account e tutti i suoi dati. Non puoi annullarla.

### Analisi di utilizzo

L'impostazione **Invia analisi d'uso anonime** attiva o disattiva le analisi anonime del prodotto.

## Passaggi successivi

- Leggi [Limiti](/docs/limits).
- Leggi le [Domande frequenti](/docs/faq).
