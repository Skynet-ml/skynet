L'app Skynet per Mac esegue Skynet sul tuo computer. Invia le chiamate ai modelli tramite una CLI di programmazione con cui hai effettuato l'accesso sul tuo Mac, quindi è il piano della tua CLI a pagare quelle chiamate. Puoi anche collegare l'app al tuo account Skynet ospitato.

## Requisiti

- Un Mac con Apple silicon. Esistono build per Intel, ma nessuno le ha provate su hardware Intel.
- Docker Desktop, in esecuzione, con **Use Rosetta for x86_64/amd64 emulation** attivato.
- Una CLI di programmazione, con accesso effettuato:
  - Claude Code (`claude`) 2.1.226 o successiva, oppure
  - Codex (`codex`) 0.160.0 o successiva.

> [!WARNING]
> Codex deve usare l'accesso con ChatGPT. Un accesso con una chiave API non è valido. Non è valido nemmeno un login conservato nel Portachiavi di macOS.

### Configurare Codex

Codex deve conservare le sue credenziali in un file. Completa questi passaggi prima del primo avvio:

1. Apri `~/.codex/config.toml` e aggiungi questa riga:

```toml
cli_auth_credentials_store = "file"
```

2. Accedi con il tuo account ChatGPT:

```bash
codex login
```

## Installare l'app

Le build hanno una firma ad hoc e non sono notarizzate. macOS le blocca finché non rimuovi il flag di quarantena.

1. Apri il file `.dmg`.
2. Trascina **Skynet** in **Applicazioni**.
3. Esegui questo comando nel Terminale:

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. Apri Skynet da **Applicazioni**.

> [!NOTE]
> Il primo avvio richiede alcuni minuti. L'app crea un'immagine sandbox locale e prepara il suo database.

## Che cosa succede all'avvio

L'app esegue questi passaggi in ordine:

1. Legge il `PATH` dalla tua shell di login, così può trovare Docker e la tua CLI.
2. Controlla Docker e la tua CLI di programmazione.
3. Avvia un database Postgres locale in Docker.
4. Crea l'immagine sandbox `skynet-local-sandbox` per `linux/amd64`. Docker la esegue con Rosetta.
5. Aggiorna lo schema del database.
6. Avvia l'API locale e l'app web, e mostra l'app web nella finestra.

### Controlli iniziali

L'app mostra un elenco di controlli: **Docker**, **Coding CLI**, **Signed in**, **Usage limit**, **Local services** e **Account**. Se un controllo non riesce, l'app mostra una schermata che ti dice cosa fare:

| Schermata                       | Che cosa fare                                                                         |
| ------------------------------- | ------------------------------------------------------------------------------------- |
| **Install Docker Desktop**      | Installa Docker Desktop.                                                              |
| **Start Docker**                | Avvia Docker Desktop.                                                                 |
| **Install a coding CLI**        | Installa Claude Code o Codex.                                                         |
| **Update your coding CLI**      | Aggiorna la CLI alla versione minima indicata sopra.                                  |
| **Sign in to your CLI**         | Accedi alla CLI nel Terminale.                                                        |
| **Usage limit reached**         | Attendi l'orario di ripristino. I nuovi job possono ripartire dopo il ripristino.     |

Fai clic su **Check again** dopo aver risolto il problema. Fai clic su **Open logs** per vedere i file di log. Nella schermata **Usage limit reached**, puoi fare clic su **Open Skynet anyway**.

## Esecuzioni locali

Nell'app, le chiamate ai modelli passano dalla CLI sul tuo Mac. Il menu dei modelli mostra questa modalità come **Locale**. Il suggerimento dice: «Le chiamate le paga il tuo piano, quindi questo modello aggiunge 0 $ all'esecuzione.»

Nell'app i nomi delle CLI sono **Claude Code** e **Codex**.

> [!NOTE]
> L'app rimuove le chiavi API dall'ambiente dei processi che avvia. Così la CLI usa il tuo abbonamento, non una chiave API.

### Eseguire il proponente sulla tua CLI

L'impostazione **Esegui il proponente sulla mia CLI** controlla dove viene eseguito l'agente proponente. Le scelte sono **Motore dell'agente (predefinito)** e **Disattivato (proponente isolato)**.

**Accesso del proponente** stabilisce che cosa può toccare la CLI:

- **Solo area di lavoro** è il valore predefinito. Si affida alla sandbox propria di ogni CLI.
- **Accesso completo** esegue la CLI con i tuoi privilegi utente.

> [!WARNING]
> Con **Accesso completo**, la CLI può leggere, modificare ed eliminare qualsiasi file a cui hai accesso tu, e raggiungere la rete. La modalità acceptEdits di Claude non isola la rete, nemmeno con **Solo area di lavoro**.

### Etichettatura nell'app

L'etichettatura con l'IA nell'app per Mac richiede la chiave del tuo provider. Scegli un modello in **La tua chiave**. Vedi [Etichettare i dati con il tagger](/docs/guides/tagger).

## Collegare l'app al tuo account Skynet

Puoi usare l'app senza un account. Per farlo, fai clic su **Use without signing in**. Per sincronizzare con il tuo account ospitato, collega l'app.

### Collegare questo Mac

1. Al passaggio **Sign in to Skynet**, l'app apre la pagina **Link a Mac** nel tuo browser predefinito.
2. Accedi a Skynet nel browser, se te lo chiede.
3. Controlla l'elenco sotto **Una volta collegato, questo Mac potrà:**.
4. Fai clic su **Collega questo Mac**. La pagina mostra «Ritorno all’app Mac…».

Se l'accesso non riesce o lo annulli, l'app mostra **Try again** e **Use without signing in**.

Una volta collegato, il Mac può:

- Vedere il tuo nome e la tua email.
- Vedere il tuo saldo.
- Sincronizzare le tue esecuzioni con il tuo account.
- Usare i modelli ospitati, pagati dal tuo saldo.
- Usare le tue origini dati collegate.
- Cercare e navigare sul web tramite Skynet.

Il collegamento usa un flusso di accesso sicuro (OAuth con PKCE). L'app conserva i suoi token cifrati sul tuo Mac. Quando scolleghi il Mac, Skynet revoca i token.

### Limiti del collegamento

| Limite                                | Valore                                     |
| ------------------------------------- | ------------------------------------------ |
| Mac collegati per ogni account        | 10                                         |
| Durata del token di accesso           | 1 ora, poi l'app lo rinnova                |
| Timeout di inattività del collegamento | 30 giorni senza utilizzo                  |
| Durata massima del collegamento       | 365 giorni                                 |

Dopo il timeout di inattività o la durata massima, collega di nuovo il Mac.

### Stato del collegamento

L'app mostra uno di questi stati: **Modalità locale**, **Collegato a questo Mac**, **Accedi per sincronizzare** o **Collegato (offline)**.

## Servizi e dati locali

Tutti i servizi locali sono in ascolto solo su `127.0.0.1`:

| Servizio   | Intervallo di porte |
| ---------- | ------------------- |
| API        | Da 48100 a 48119    |
| App web    | Da 48120 a 48139    |
| Postgres   | Da 48140 a 48159    |

L'app conserva i suoi dati in `~/Library/Application Support/Skynet`. La cartella `logs/` contiene `desktop.log`, `api.log`, `frontend.log`, `migrate.log` e `sandbox-build.log`.

Quando chiudi l'app, questa arresta l'app web, l'API, Postgres e tutti i container dei job.

### Reimpostare l'app

> [!WARNING]
> Questi comandi eliminano tutti i dati locali, incluse le tue esecuzioni e i tuoi dataset locali.

Chiudi prima l'app. Poi esegui:

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## Passaggi successivi

- [Avvia un'esecuzione](/docs/guides/submit).
- Leggi la pagina sulla [fatturazione](/docs/concepts/billing) per i modelli ospitati.
- Leggi [Sicurezza](/docs/security).
