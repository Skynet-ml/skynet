Skynet imposta limiti su esecuzioni, file, spazio di archiviazione e frequenza delle richieste. I limiti mantengono stabile la piattaforma e proteggono il tuo saldo. Questa pagina elenca i valori predefiniti. Un amministratore può modificare alcuni limiti per un account.

> [!NOTE]
> I valori in questa pagina sono quelli predefiniti attuali. Quando raggiungi un limite, l'app o l'API ti indica quale limite ha bloccato la richiesta.

## Esecuzioni

| Limite                              | Free | Pro   |
| ----------------------------------- | ---- | ----- |
| Esecuzioni attive contemporanee     | 5    | 20    |
| Esecuzioni salvate                  | 100  | 1.000 |

Un'esecuzione attiva è un'esecuzione con stato `pending`, `validating`, `running` o `paused`. Quando hai il numero massimo di esecuzioni attive, Skynet rifiuta nuove esecuzioni finché una di esse non termina.

Il limite di esecuzioni salvate conta tutte le tue esecuzioni. Elimina le esecuzioni che non ti servono per fare spazio a quelle nuove.

Se il rinnovo di Pro non va a buon fine, l'account mantiene i limiti Pro per 7 giorni. Dopo, usa i limiti Free finché il pagamento non va a buon fine.

| Limite                                              | Valore                                                                   |
| --------------------------------------------------- | ------------------------------------------------------------------------ |
| Copie in una richiesta di clonazione tramite API    | Da 1 a 5                                                                 |
| Tentativi di ripristino automatico per un'esecuzione | 3                                                                       |
| Casi di esempio nel controllo del contratto         | 3                                                                        |
| Eventi di avanzamento salvati per ogni esecuzione   | 5.000. Skynet rimuove prima gli eventi più vecchi.                       |
| Durata di una sandbox                               | 45 minuti. Un'esecuzione che richiede più tempo apre una nuova sandbox.  |

Leggi [Esecuzioni](/docs/concepts/runs) per sapere come funzionano i tentativi di ripristino.

## Archiviazione

| Limite                        | Free    | Pro    |
| ----------------------------- | ------- | ------ |
| Spazio di archiviazione totale | 500 MiB | 20 GiB |

Lo spazio di archiviazione totale comprende esecuzioni, dataset, log, chat con l'agente, caricamenti in attesa e dati di ricerca. Skynet rifiuta una scrittura che supererebbe il tuo limite di archiviazione.

## File e dataset

| Limite                                              | Valore                                     |
| --------------------------------------------------- | ------------------------------------------ |
| File di dataset salvato nella tua libreria          | 50 MiB per ogni file, dopo la compressione |
| Righe da un connettore che importa record           | 20.000                                     |
| File allegato a una chat con l'agente               | 20 MiB                                     |
| Testo letto da un allegato della chat               | 50.000 caratteri                           |
| Clip audio per la dettatura                         | 25 MB                                      |
| Archivio di un repository GitHub                    | 200 MiB, compresso                         |
| File del repository aperto nella pagina dell'esecuzione | 1 MiB                                  |

I connettori che importano record includono tabelle di database, tracce LLM e database Notion. Leggi [Preparare un dataset](/docs/guides/datasets).

### File del workspace nella procedura guidata

| Limite                          | Valore                                                                  |
| ------------------------------- | ----------------------------------------------------------------------- |
| File aggiuntivi nel workspace   | 20                                                                      |
| Lunghezza del nome di un file   | 120 caratteri                                                           |
| Profondità delle cartelle       | 3 cartelle                                                              |
| Tipi di file                    | `.py`, `.txt`, `.md`, `.json`, `.yaml`, `.csv`, `.toml`, `.ini`, `.cfg` |

`run.py` e `metric.py` sono i file di ingresso di un'esecuzione. Non puoi eliminarli, rinominarli o spostarli.

## Frequenza delle richieste

| Limite                                                                     | Valore                                              |
| -------------------------------------------------------------------------- | --------------------------------------------------- |
| Invii di esecuzioni                                                        | 30 per ogni account in un minuto mobile             |
| Clip di dettatura                                                          | 120 per ogni account in un'ora                      |
| File letti per le chat con l'agente                                        | 300 per ogni account in un'ora                      |
| Richieste sull'account (registrazione, reimpostazione password, verifica email) | 20 per ogni indirizzo email in un'ora mobile   |
| Turni dell'assistente della procedura guidata                              | 50 per ogni account in un giorno UTC                |
| Token di ricerca e indicizzazione                                          | 2.000.000 per ogni account in un mese               |

Quando superi un limite di frequenza, l'API restituisce lo stato HTTP `429`. Attendi, poi invia di nuovo la richiesta.

Quando un account supera i suoi token mensili di ricerca e indicizzazione, Skynet smette di indicizzare nuovi elementi per quell'account. La ricerca funziona poi come se la ricerca semantica non fosse disponibile.

## Denaro

| Limite                                                        | Valore                                                                            |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Limite di spesa predefinito per una nuova esecuzione          | 5 $, oppure l'importo necessario per l'avvio se è maggiore                        |
| Margine aggiuntivo nell'importo necessario per l'avvio        | 1 $, oppure il costo della singola chiamata al modello più costosa se è maggiore  |
| Ricarica personalizzata                                       | Da 5 $ a 1.000 $                                                                  |
| Saldo necessario per avviare un turno interattivo del modello | 0,05 $                                                                            |

Leggi [Budget e fatturazione](/docs/concepts/billing) per commissioni e riserve.

## Pagine correlate

- [Esecuzioni](/docs/concepts/runs)
- [Budget e fatturazione](/docs/concepts/billing)
- [Domande frequenti](/docs/faq)
