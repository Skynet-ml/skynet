Skynet migliora un artefatto che hai già. Gli fornisci una versione iniziale, un insieme di casi e una metrica. Skynet propone nuove versioni, esegue ciascuna sui tuoi casi, assegna un punteggio all'output con la tua metrica e conserva la versione migliore.

## Che cosa ottimizza Skynet

Skynet lavora su due tipi di artefatto:

| Tipo di artefatto | Che cosa cambia                                                     |
| ----------------- | ------------------------------------------------------------------- |
| Repository | I file nei percorsi che consenti di modificare |
| Programma DSPy    | Le istruzioni dei predictor in un `dspy.Module`                     |

Ogni esecuzione usa lo stesso contratto. Il tuo codice di esecuzione definisce `run(artifact, input)`. La tua metrica definisce `score(input, output, expected=None)`. La metrica restituisce un punteggio e un feedback scritto. L'ottimizzatore legge il feedback per migliorare la versione successiva.

> [!NOTE]
> Il codice di esecuzione e la metrica girano in una sandbox, in processi separati. Il codice di esecuzione non può vedere il sorgente della metrica né i valori attesi.

## Un solo prodotto, molti motori

Skynet è Optimize Anything. Il tipo di artefatto determina quali motori possono lavorarci:

- I repository usano i motori nativi: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler e ShinkaEvolve.
- Gli artefatti Programma DSPy usano gli ottimizzatori DSPy: DSPy GEPA, DSPy MIPROv2 e DSPy BootstrapFewShot.

Su un programma DSPy, **Auto** usa DSPy GEPA. Su un repository, scegli un motore nativo. Leggi [Come funziona l'ottimizzazione](/docs/concepts/optimization) e [Motori](/docs/concepts/engines).

## Come funziona un'esecuzione

1. Descrivi l'attività e imposti la versione iniziale.
2. Fornisci i casi. Skynet li suddivide in set di addestramento, convalida e test.
3. Selezioni un ottimizzatore, i modelli e un budget.
4. Scrivi il codice di esecuzione e la metrica. Un controllo del contratto li esegue su alcuni casi di esempio.
5. Skynet esegue l'ottimizzazione e mostra i risultati.

Skynet seleziona le versioni sui casi di convalida. Riporta il punteggio finale sui casi di test. I motori non vedono mai i casi di test.

## Da dove iniziare

| Se vuoi                                           | Leggi                                                         |
| ------------------------------------------------- | ------------------------------------------------------------- |
| Avviare la tua prima esecuzione                   | [Avvio rapido](/docs/quickstart)                              |
| Capire come funziona l'ottimizzazione             | [Come funziona l'ottimizzazione](/docs/concepts/optimization) |
| Scegliere un motore                               | [Motori](/docs/concepts/engines)                              |
| Preparare i dati                                  | [Dataset e suddivisioni](/docs/concepts/datasets)             |
| Scrivere una metrica                              | [Metriche e scorer](/docs/concepts/scorers)                   |
| Compilare la procedura guidata                    | [Avviare un'esecuzione](/docs/guides/submit)                  |
| Leggere i risultati                               | [Risultati](/docs/guides/results)                             |
| Capire i costi                                    | [Fatturazione](/docs/concepts/billing)                        |
| Usare Skynet da un agente                         | [MCP](/docs/mcp)                                              |
| Chiamare Skynet dal codice                        | [API](/docs/api)                                              |
| Controllare i limiti                              | [Limiti](/docs/limits)                                        |

> [!TIP]
> Inizia con un dataset piccolo e le impostazioni predefinite. Rendi specifico il feedback della metrica. Un buon feedback dà all'ottimizzatore molte più informazioni del solo punteggio.
