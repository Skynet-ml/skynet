Un motore è il metodo di ricerca che propone nuove versioni del tuo artefatto. Skynet ha sei motori nativi per i repository, tre ottimizzatori DSPy per i programmi DSPy e **Auto**. Questa pagina spiega cosa fa ciascuno, quando sceglierlo e quali impostazioni ha.

Leggi prima [Come funziona l'ottimizzazione](/docs/concepts/optimization) se non sai quali motori si adattano al tuo artefatto.

## Confronto rapido

| Motore                | Tipi di artefatto     | Impostazioni dell'agente di programmazione | Impostazione delle iterazioni massime |
| --------------------- | --------------------- | ------------------------------------------ | ------------------------------------- |
| Auto | Programma DSPy | No | No |
| GEPA | Repository | No | No |
| Best-of-N | Repository | No | No |
| AutoResearch | Repository | Sì | No |
| Meta-Harness | Repository | Sì | Sì |
| AutoSaddler | Repository | Sì | Sì |
| ShinkaEvolve | Repository | No. Ha una propria impostazione dell'editor. | Sì |
| DSPy GEPA             | Programma DSPy        | No                                         | No                                    |
| DSPy MIPROv2          | Programma DSPy        | No                                         | No                                    |
| DSPy BootstrapFewShot | Programma DSPy        | No                                         | No                                    |

La procedura guidata contrassegna GEPA, DSPy GEPA e Auto come ottimizzatori che usano il testo del feedback della tua metrica.

## Auto

Auto è l'opzione predefinita. Che cosa esegue dipende dal tipo di artefatto:

- **Programma DSPy**: Auto esegue DSPy GEPA.
- **Repository**: Auto non è disponibile. Scegli un motore nativo.

**Sceglilo quando** non sai quale motore sia adatto. È il punto di partenza migliore.

## GEPA

«Evoluzione riflessiva con un fronte di Pareto di versioni.» Il modello di riflessione legge il punteggio e il feedback di ogni caso e scrive una versione migliore. GEPA mantiene un fronte di versioni, ciascuna la migliore su alcuni casi.

**Sceglilo quando** la tua metrica fornisce un feedback specifico e vuoi un singolo motore potente.

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

Un agente di programmazione legge il feedback della tua metrica e applica patch alla versione. Skynet conserva solo le patch che una nuova esecuzione dello scorer conferma.

**Sceglilo quando** il feedback della tua metrica indica difetti chiari e localizzati.

## ShinkaEvolve

«Fa evolvere molte versioni in isole parallele e impara quali modelli le migliorano di più.» È una ricerca evolutiva su una popolazione di candidati, con mutazioni e incroci scritti da un modello.

**Sceglilo quando** vuoi un'esplorazione ampia e hai un budget più grande.

## Ottimizzatori DSPy

| Ottimizzatore         | Che cosa fa                                                                                                                         |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| DSPy GEPA             | Legge il feedback della metrica e fa evolvere le istruzioni di ogni predictor.                                                      |
| DSPy MIPROv2          | Propone insieme istruzioni e demo few-shot, e ne cerca le combinazioni con l'ottimizzazione bayesiana.                              |
| DSPy BootstrapFewShot | Raccoglie esempi svolti su cui il programma risponde correttamente e li aggiunge come demo few-shot. Non usa un modello di riflessione. |

DSPy MIPROv2 e DSPy BootstrapFewShot richiedono coppie (casi con output attesi).

## Impostazioni

### Budget

| Impostazione                  | Predefinito | Intervallo                                                          |
| ----------------------------- | ----------- | ------------------------------------------------------------------- |
| Esecuzioni dello scorer max   | 200         | Da 1 a 100000                                      |
| Fermati al punteggio          | Disattivato | 0 o più                                                             |
| Iterazioni max                | Disattivato | Da 1 a 1000. Solo Meta-Harness, AutoSaddler e ShinkaEvolve.         |

Un'esecuzione dello scorer è una chiamata della tua metrica su un caso. Il ricalcolo finale del punteggio della versione iniziale e del vincitore non viene conteggiato in **Esecuzioni dello scorer max**.

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
