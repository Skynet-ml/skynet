Skynet migliora il codice che hai già, o il codice che scrive per te. Skynet è Optimize Anything: ogni esecuzione ottimizza un repository. Gli fornisci un repository, un insieme di casi e uno scorer. Skynet propone nuove versioni del repository, assegna un punteggio a ciascuna con il tuo scorer e conserva la versione migliore.

## Che cosa ottimizza Skynet

Ogni esecuzione ottimizza un repository. Puoi iniziare in due modi:

| Inizio                    | Che cosa succede                                                                                 |
| ------------------------- | ------------------------------------------------------------------------------------------------ |
| Usa un repository         | Skynet modifica i file nei percorsi che consenti, in un repository GitHub che colleghi.          |
| Crea un nuovo repository  | Fornisci un'attività e dei dati. Skynet crea un nuovo repository GitHub nel tuo account e scrive una prima soluzione. |

Il codice nel repository può essere qualsiasi cosa: un'applicazione, un servizio, uno script o una libreria. Può usare qualsiasi libreria.

Ogni esecuzione usa lo stesso contratto. Il tuo scorer definisce `score(repo_path, case)`. Restituisce un punteggio e un feedback scritto. L'ottimizzatore legge il feedback per migliorare la versione successiva.

> [!NOTE]
> Ogni versione gira in una sandbox. L'agente di programmazione che scrive le versioni non può vedere il sorgente dello scorer né i valori attesi.

## Un solo prodotto, molti motori

**Auto** è il valore predefinito per ogni esecuzione. Prova GEPA, AutoResearch, Meta-Harness, AutoSaddler e ShinkaEvolve, poi continua GEPA dalla versione migliore. Puoi anche selezionare un solo motore: GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler o ShinkaEvolve. Leggi [Come funziona l'ottimizzazione](/docs/concepts/optimization) e [Motori](/docs/concepts/engines).

## Come funziona un'esecuzione

1. Descrivi l'attività e scegli un repository, oppure chiedi a Skynet di crearne uno.
2. Fornisci i casi. Skynet li suddivide in set di addestramento, convalida e test.
3. Selezioni un ottimizzatore, i modelli e un budget.
4. Scrivi lo scorer. Un controllo di configurazione lo esegue sulla versione iniziale.
5. Skynet esegue l'ottimizzazione e mostra i risultati.

Skynet seleziona le versioni sui casi di convalida. Riporta il punteggio finale sui casi di test. I motori non vedono mai i casi di test.

## Da dove iniziare

| Se vuoi                                           | Leggi                                                         |
| ------------------------------------------------- | ------------------------------------------------------------- |
| Avviare la tua prima esecuzione                   | [Avvio rapido](/docs/quickstart)                              |
| Capire come funziona l'ottimizzazione             | [Come funziona l'ottimizzazione](/docs/concepts/optimization) |
| Scegliere un motore                               | [Motori](/docs/concepts/engines)                              |
| Preparare i dati                                  | [Dataset e suddivisioni](/docs/concepts/datasets)             |
| Scrivere uno scorer                               | [Metriche e scorer](/docs/concepts/scorers)                   |
| Compilare la procedura guidata                    | [Avviare un'esecuzione](/docs/guides/submit)                  |
| Leggere i risultati                               | [Risultati](/docs/guides/results)                             |
| Capire i costi                                    | [Fatturazione](/docs/concepts/billing)                        |
| Usare Skynet da un agente                         | [MCP](/docs/mcp)                                              |
| Chiamare Skynet dal codice                        | [API](/docs/api)                                              |
| Controllare i limiti                              | [Limiti](/docs/limits)                                        |

> [!TIP]
> Inizia con un dataset piccolo e le impostazioni predefinite. Rendi specifico il feedback dello scorer. Un buon feedback dà all'ottimizzatore molte più informazioni del solo punteggio.
