Un motore è il metodo di ricerca che propone nuove versioni del tuo repository. Skynet ha sei motori nativi e **Auto**. Ogni motore lavora sui repository. Questa pagina spiega cosa fa ciascuno, quando sceglierlo e quali impostazioni ha.

Leggi prima [Come funziona l'ottimizzazione](/docs/concepts/optimization) se non sai quale motore si adatta alla tua attività.

## Confronto rapido

| Motore       | Impostazioni dell'agente di programmazione   | Impostazione delle iterazioni massime |
| ------------ | -------------------------------------------- | ------------------------------------- |
| Auto         | No                                           | No                                    |
| GEPA         | No                                           | No                                    |
| Best-of-N    | No                                           | No                                    |
| AutoResearch | Sì                                           | No                                    |
| Meta-Harness | Sì                                           | Sì                                    |
| AutoSaddler  | Sì                                           | Sì                                    |
| ShinkaEvolve | No. Ha una propria impostazione dell'editor. | Sì                                    |

La procedura guidata contrassegna GEPA e Auto come ottimizzatori che usano il testo del feedback del tuo scorer.

## Auto

Auto è il valore predefinito per ogni esecuzione. Lavora in due fasi:

1. Esplora con GEPA, AutoResearch, Meta-Harness, AutoSaddler e ShinkaEvolve.
2. Continua GEPA dalla versione migliore che l'esplorazione ha trovato.

**Sceglilo quando** non sai quale motore sia adatto. È il punto di partenza migliore.

## GEPA

«Evoluzione riflessiva con un fronte di Pareto di versioni.» Il modello di riflessione legge il punteggio e il feedback di ogni caso e scrive una versione migliore. GEPA mantiene un fronte di versioni, ciascuna la migliore su alcuni casi.

**Sceglilo quando** il tuo scorer fornisce un feedback specifico e vuoi un singolo motore potente.

## Best-of-N

«Proposte indipendenti dal modello di riflessione; conserva la migliore.» Ogni proposta è indipendente dalle altre. Skynet ricalcola il punteggio delle proposte migliori sui casi di convalida.

**Sceglilo quando** vuoi una baseline semplice da confrontare con altri motori.

## AutoResearch

Un agente di programmazione esegue cicli di ricerca. Modifica, valuta e conserva ciò che aiuta finché il budget non si esaurisce.

**Sceglilo quando** una buona modifica richiede diversi passaggi di indagine.

## Meta-Harness

Un agente di programmazione propone un gruppo di candidati a ogni iterazione. Usa l'intera cronologia dei candidati e dei punteggi.

**Sceglilo quando** vuoi un agente che impari dalla cronologia di tutti i tentativi precedenti. Usa **Candidati per iterazione** per impostare la dimensione del gruppo.

## AutoSaddler

Un agente di programmazione legge il feedback del tuo scorer e applica patch alla versione. Skynet conserva solo le patch che una nuova esecuzione dello scorer conferma.

**Sceglilo quando** il feedback del tuo scorer indica difetti chiari e localizzati.

## ShinkaEvolve

«Fa evolvere molte versioni in isole parallele e impara quali modelli le migliorano di più.» È una ricerca evolutiva su una popolazione di candidati, con mutazioni e incroci scritti da un modello.

**Sceglilo quando** vuoi un'esplorazione ampia e hai un budget più grande.

## Impostazioni

### Budget

| Impostazione                  | Predefinito | Intervallo                                                          |
| ----------------------------- | ----------- | ------------------------------------------------------------------- |
| Esecuzioni dello scorer max   | 200         | Da 1 a 100000                                      |
| Fermati al punteggio          | Disattivato | 0 o più                                                             |
| Iterazioni max                | Disattivato | Da 1 a 1000. Solo Meta-Harness, AutoSaddler e ShinkaEvolve.         |

Un'esecuzione dello scorer è una chiamata del tuo scorer su un caso. Il ricalcolo finale del punteggio della versione iniziale e del vincitore non viene conteggiato in **Esecuzioni dello scorer max**.

### Impostazioni dell'agente di programmazione

Queste impostazioni si applicano ad AutoResearch, Meta-Harness e AutoSaddler.

| Impostazione                         | Predefinito | Intervallo                                          |
| ------------------------------------ | ----------- | --------------------------------------------------- |
| Agente di programmazione             | Codex       | Pi, Codex, OpenCode, Prime Agent, Comando personalizzato |
| Candidati per iterazione             | 3           | Da 1 a 8. |
| Chiamate di strumenti max per candidato | 100      | Da 1 a 1000                                         |

### Impostazioni di ShinkaEvolve

| Impostazione                                         | Predefinito                                                                       |
| ---------------------------------------------------- | --------------------------------------------------------------------------------- |
| Come vengono scritte le nuove versioni               | Singola chiamata al modello. L'altra opzione è «Agente di programmazione (Pi)».   |
| Isole                                                | 2 (da 1 a 8)                                                                      |
| Intervallo di migrazione                             | 10                                                                                |
| Selezione dei genitori                               | Ponderata. Altre opzioni: Legge di potenza, Beam search.                          |
| Modifiche diff / Riscritture complete / Incroci      | 60% / 30% / 10%. La somma deve essere 100%.                                       |
| Dimensione dell'archivio                             | 40                                                                                |
| Valutazioni parallele                                | 2                                                                                 |
| Proposte parallele                                   | 2                                                                                 |

## Pagine correlate

- [Come funziona l'ottimizzazione](/docs/concepts/optimization)
- [Metriche e scorer](/docs/concepts/scorers)
- [Esecuzioni](/docs/concepts/runs)
- [Fatturazione](/docs/concepts/billing)
