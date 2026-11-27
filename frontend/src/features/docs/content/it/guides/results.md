La pagina dell'esecuzione mostra l'avanzamento e i risultati di un'esecuzione. Questa guida spiega le schede della pagina dell'esecuzione, i punteggi e come esportare la versione migliore.

## Schede della pagina dell'esecuzione

La pagina dell'esecuzione mostra solo le schede pertinenti all'esecuzione. Alcune schede dipendono dallo stato e dal tuo livello di esperienza.

| Scheda                     | Quando compare                                                   | Che cosa contiene                                                                  |
| -------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **Panoramica**             | Sempre                                                           | Avanzamento, punteggi e cronologia dei punteggi                                    |
| **Artefatto**              | Un'esecuzione con un risultato                                   | Le versioni del tuo repository, con la versione migliore                           |
| **Dati**                   | Un'esecuzione terminata con casi                                 | I dati suddivisi in set di addestramento, convalida e test                         |
| **Codice**                 | Quando l'esecuzione ha codice da mostrare                        | Lo scorer                                                                          |
| **Log**                    | La maggior parte delle esecuzioni                                | Il log dell'esecuzione                                                             |
| **Utilizzo e costi**       | Quando hai effettuato l'accesso                                  | Quanto ha speso l'esecuzione                                                       |
| **Esecuzione pianificata** | Un'esecuzione riuscita                                           | Una pianificazione per eseguire di nuovo l'ottimizzazione dalla versione migliore |
| **Configurazione**         | Sempre                                                           | La configurazione dell'esecuzione                                                  |

> [!NOTE]
> Al livello **Guidato**, la pagina dell'esecuzione nasconde le schede **Dati**, **Codice**, **Log** e **Configurazione**. Un link diretto a una di queste schede la apre comunque. Cambia il livello in **Impostazioni → Account**.

## Punteggi

Skynet seleziona le versioni sui casi di convalida. Riporta il punteggio finale sui casi di test. I motori non vedono mai i casi di test. Leggi [Dataset e suddivisioni](/docs/concepts/datasets).

La scheda **Panoramica** mostra:

- **Miglioramento**: la variazione dalla versione iniziale alla versione migliore.
- **Progressione del punteggio**: il punteggio nel corso dell'esecuzione.
- **Dettaglio del punteggio**: ogni punteggio, con le colonne **Baseline**, **Ottimizzato** e **Variazione**.

Mentre l'esecuzione è attiva, la panoramica mostra anche **Trascorso**, **Rimanente** e **Velocità**.

La panoramica mostra anche **Punteggi con nome** quando il tuo scorer riporta punteggi con nome.

### Quando la versione migliore è la versione iniziale

A volte nessun candidato supera la versione iniziale. In questo caso Skynet restituisce la versione iniziale. La scheda **Artefatto** ti informa che è stata applicata una protezione contro le regressioni.

Se l'esecuzione si è fermata al suo budget, la pagina dell'esecuzione ti indica quale versione Skynet ha salvato. Leggi [Esecuzioni](/docs/concepts/runs).

## Esportare la versione migliore

La scheda **Artefatto** elenca le **Versioni** del tuo repository. Per ogni versione puoi:

- Copiare la versione.
- Scaricare la versione come file.
- Scaricare la versione come zip dell'intero repository.

Puoi confrontare una versione con un'altra. La scheda mostra anche il **Feedback dello scorer** di una versione.

### Branch in un nuovo repository

Quando Skynet crea il repository per l'esecuzione su GitHub, invia ogni candidato a un proprio branch, `skynet/candidate-<version id>`. Il branch `skynet/best` punta sempre alla versione migliore. Si sposta quando compare una versione migliore.

## Condividere i risultati

Puoi condividere un'esecuzione con altre persone, oppure elencarla nella galleria pubblica Esplora. Leggi [Condividere e pubblicare](/docs/guides/sharing).
