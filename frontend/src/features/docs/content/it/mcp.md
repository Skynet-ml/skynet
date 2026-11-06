Il server MCP di Skynet permette a un agente IA di usare Skynet al posto tuo. Claude, ChatGPT, Codex, Cursor e altri client MCP possono controllare la configurazione di un'esecuzione, avviare esecuzioni, seguirle e arrestarle. Approvi la connessione una sola volta nel browser. L'agente imposta un limite di spesa per ogni esecuzione.

## URL del server

Tutti i client usano lo stesso URL:

```text
https://mcp.skynetml.com/mcp
```

Il server usa Streamable HTTP. L'accesso usa OAuth 2.1. Non devi copiare alcun token o chiave API nel client.

## Collegare il client

Seleziona il tuo client. Segui i suoi passaggi, poi vai a [Approvare la connessione](#approvare-la-connessione).

```skynet-mcp-clients

```

## Approvare la connessione

La prima volta che l'agente chiama Skynet, il browser apre una pagina di Skynet.

1. Accedi a Skynet, se non hai già effettuato l'accesso.
2. Leggi la pagina. Mostra il nome dell'agente, il sito a cui ritorna e il tuo account.
3. Leggi che cosa può fare l'agente:
   - Vedere le tue esecuzioni di ottimizzazione e i loro risultati.
   - Avviare e arrestare esecuzioni di ottimizzazione, pagate dal tuo saldo.
4. Fai clic su **Allow** per collegarlo. Fai clic su **Deny** per fermarti.

Dopo che hai fatto clic su **Allow**, il browser torna all'agente. Ora l'agente può chiamare gli strumenti elencati sotto.

> [!NOTE]
> L'agente imposta un limite di spesa per ogni esecuzione. Non può aggiungere fondi.

## Strumenti

| Strumento   | Che cosa fa                                                                                        | Accesso   |
| ----------- | -------------------------------------------------------------------------------------------------- | --------- |
| `check_run` | Controlla la configurazione di un'esecuzione senza avviarla né addebitarti nulla. Suggerisce un limite di spesa. | Lettura   |
| `start_run` | Avvia un'esecuzione con un limite di spesa.                                                        | Scrittura |
| `get_run`   | Restituisce lo stato, la spesa e, quando l'esecuzione termina, il risultato.                       | Lettura   |
| `list_runs` | Elenca le tue esecuzioni, dalla più recente. Può filtrare per stato.                               | Lettura   |
| `stop_run`  | Arresta un'esecuzione. Una seconda chiamata restituisce un errore che indica che l'esecuzione è già terminata. | Scrittura |

L'accesso in lettura usa lo scope `runs:read`. L'accesso in scrittura usa lo scope `runs:write`.

Nessuno strumento aggiunge fondi, modifica il tuo saldo o cambia le impostazioni di fatturazione.

## Come un agente esegue un'ottimizzazione

1. L'agente chiama `check_run` con la configurazione dell'esecuzione. Skynet esegue i controlli statici e restituisce:
   - Il risultato di ogni controllo.
   - Il limite di spesa minimo con cui l'esecuzione può partire.
   - Un limite di spesa suggerito. È di almeno 5,00 $.
   - I modelli senza un prezzo noto.
   - L'importo che puoi spendere adesso.
2. L'agente chiama `start_run` con un limite di spesa (`max_cost_cents`, da 1 a 1.000.000 centesimi di dollaro USA). Può anche inviare una `idempotency_key`, così un nuovo tentativo non avvia una seconda esecuzione.
3. Se il controllo della configurazione è ancora in corso, `start_run` restituisce `in_progress` e `retry_after_seconds`. Trascorso quel tempo, l'agente chiama di nuovo `start_run` con lo stesso input.
4. L'agente chiama `get_run` finché l'esecuzione non termina. Gli stati finali sono `success`, `failed`, `cancelled` e `stopped`.

> [!TIP]
> Indica il tuo limite di spesa all'agente nel prompt, ad esempio «Ottimizza questo repository. Spendi al massimo 10 $.» L'agente userà quel limite in `start_run`.

L'esecuzione si arresta quando raggiunge il suo limite di spesa. Leggi [Fatturazione](/docs/concepts/billing) per sapere come Skynet addebita un'esecuzione.

## Sicurezza

- Ogni client riceve una propria autorizzazione. Il client non vede mai la tua password.
- L'accesso usa PKCE (S256). Il client si registra tramite registrazione dinamica del client o un documento di metadati del client ID.
- I token di accesso scadono dopo 1 ora. Il client li rinnova con un refresh token.
- Un refresh token scade dopo 30 giorni di inutilizzo. Un'autorizzazione dura al massimo 90 giorni. Dopo devi approvare di nuovo la connessione.
- Ogni refresh token funziona una sola volta. Se qualcuno riusa un vecchio refresh token, Skynet revoca l'autorizzazione.
- L'agente può vedere e modificare solo le tue esecuzioni.

Leggi [Sicurezza](/docs/security) per sapere come Skynet protegge il tuo account e i tuoi dati.

## Disconnettere un agente

Per disconnettere un agente, rimuovi il server Skynet dal client. La maggior parte dei client revoca poi i propri token. Se un client non li revoca, il suo refresh token scade dopo 30 giorni di inutilizzo.

## Risoluzione dei problemi

| Problema                                       | Che cosa fare                                                                    |
| ---------------------------------------------- | -------------------------------------------------------------------------------- |
| La connessione fallisce subito                 | Controlla l'URL del server. Deve essere esattamente `https://mcp.skynetml.com/mcp`. |
| «This agent can't connect»                     | Avvia di nuovo la connessione dal client.                                        |
| «Too many attempts»                            | Attendi qualche minuto, poi riprova.                                             |
| Il client non apre un browser                  | Aggiorna il client. Deve supportare server MCP remoti con OAuth.                 |
| `start_run` fallisce con un errore di saldo    | Aggiungi fondi in Skynet, oppure avvia l'esecuzione con un limite di spesa più basso. |
| Gli strumenti smettono di funzionare dopo qualche settimana | L'autorizzazione è scaduta. Rimuovi il server e collegalo di nuovo. |

Per l'accesso HTTP diretto senza un agente, leggi [API](/docs/api).
