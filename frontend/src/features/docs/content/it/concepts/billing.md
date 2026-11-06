Skynet usa un saldo prepagato. Aggiungi fondi al saldo. Ogni esecuzione ha un limite di spesa, e Skynet addebita al tuo saldo l'uso reale dell'esecuzione. Questa pagina spiega il saldo, i budget delle esecuzioni, le riserve, le commissioni e le chiavi dei tuoi provider.

> [!NOTE]
> I numeri in questa pagina sono i valori predefiniti attuali. L'app mostra i valori aggiornati. Apri **Impostazioni → Fatturazione** per vedere il saldo, la commissione di ricarica e la cronologia delle transazioni.

## Saldo

Il tuo saldo è in dollari USA. Skynet lo conserva in centesimi.

Per impostazione predefinita Skynet non offre un saldo iniziale gratuito. Aggiungi fondi prima di avviare la tua prima esecuzione a pagamento.

Aggiungi fondi in **Impostazioni → Fatturazione**. Puoi scegliere un importo fisso o inserire un importo personalizzato:

| Ricarica          | Importo aggiunto al saldo           |
| ----------------- | ----------------------------------- |
| Importi fissi     | 5 $, 20 $ o 50 $                    |
| Personalizzata    | Qualsiasi importo da 5 $ a 1.000 $  |

L'importo che scegli è l'importo che entra nel tuo saldo. La commissione di ricarica si aggiunge a questo importo. Vedi le commissioni più sotto.

**Impostazioni → Utilizzo** mostra quanto hai speso. La cronologia delle transazioni in **Impostazioni → Fatturazione** elenca le tue ricariche e gli addebiti delle esecuzioni.

## Budget delle esecuzioni

Ogni esecuzione ha un limite di spesa. Il limite è il massimo che l'esecuzione può spendere. Copre la configurazione e l'ottimizzazione.

Un'esecuzione non ha una stima dei costi prima di partire. Spende fino al suo limite, si ferma al budget e conserva la sua versione migliore. Leggi [Esecuzioni](/docs/concepts/runs) per vedere come Skynet decide quando fermarsi.

Imposti il limite nella schermata **Budget** della procedura guidata di invio, nel campo **Limite di spesa**:

- **Imposta un limite** è la modalità predefinita. Il limite parte da 5 $. Se l'esecuzione ha bisogno di più di 5 $ per partire, il limite parte da quell'importo.
- **Nessun limite** permette all'esecuzione di attingere al tuo saldo finché non termina. L'esecuzione non va mai in pausa. Se il saldo si esaurisce, l'esecuzione si ferma e conserva ciò che ha prodotto.

### L'importo necessario per iniziare

La procedura guidata mostra l'importo **Necessario per iniziare**. Questo importo è:

- le somme trattenute per le sandbox che l'esecuzione apre prima della sua prima chiamata al modello, più
- un margine per le prime chiamate al modello. Il margine è 1 $, oppure il costo della singola chiamata più costosa dei modelli selezionati se è maggiore.

La procedura guidata non ti fa proseguire quando il limite è inferiore a questo importo. Ti blocca anche quando il tuo saldo è inferiore a questo importo. Alza il limite o aggiungi fondi per continuare.

> [!TIP]
> Quando un'esecuzione si ferma al suo budget, usa **Alza il limite e continua** nella pagina dell'esecuzione. L'esecuzione prosegue dall'ultimo checkpoint.

## Riserve

Prima di svolgere un lavoro a pagamento, Skynet riserva una parte del limite dell'esecuzione. Skynet rilascia la riserva quando il lavoro viene regolato e addebita solo il costo reale.

Quando un budget ha già delle attività, la scheda **Limite di spesa** nella procedura guidata ne mostra il registro:

| Etichetta                         | Significato                                                                                  |
| --------------------------------- | -------------------------------------------------------------------------------------------- |
| **Rimanente in questo limite**    | La parte del limite che resta.                                                               |
| **Speso finora**                  | Il costo già regolato.                                                                       |
| **Riservati**                     | Denaro trattenuto per il lavoro in corso. Skynet lo rilascia quando il lavoro viene regolato. |

## Commissioni

Skynet applica due addebiti oltre al costo del provider.

| Addebito                                   | Valore predefinito attuale                       | Si applica a                                                  |
| ------------------------------------------ | ------------------------------------------------ | ------------------------------------------------------------- |
| Commissione di ricarica                    | 12,5% dell'importo, più 0,35 $                   | Ogni ricarica                                                 |
| Ricarico sull'utilizzo                     | Costo del provider × 1,15 (costo più 15%)        | Chiamate ai modelli con le chiavi di Skynet e calcolo della sandbox |

### Commissione di ricarica

Skynet arrotonda la parte percentuale al centesimo superiore e poi aggiunge 0,35 $. Ad esempio, una ricarica di 20 $ ha una commissione di 2,85 $. La schermata di ricarica mostra la commissione prima del pagamento.

### Ricarico sull'utilizzo

Skynet moltiplica il costo del provider per il ricarico sull'utilizzo. Il ricarico si applica alle chiamate ai modelli che paga Skynet e al calcolo della sandbox. Le riserve, l'importo necessario per iniziare e i prezzi dei modelli nell'app includono già il ricarico.

Skynet determina il costo del provider di una chiamata al modello in quest'ordine:

1. Il costo che OpenRouter riporta per la chiamata.
2. Il listino prezzi aggiornato di OpenRouter per il modello.
3. La tabella prezzi di LiteLLM.
4. Una tariffa di riserva elevata di 15 $ per milione di token di input e 75 $ per milione di token di output. Questa tariffa garantisce che Skynet non addebiti mai un modello sconosciuto al di sotto del suo costo.

## Le chiavi dei tuoi provider (BYOK)

Puoi eseguire le chiamate ai modelli con la chiave del tuo provider. Aggiungi le chiavi in **Impostazioni → Provider**. Per OpenRouter puoi anche usare **Continua con OpenRouter**.

Quando una chiamata al modello usa la tua chiave:

- Il tuo provider addebita i token del modello sulla tua chiave.
- Skynet non applica costi per il modello.
- Il budget dell’esecuzione copre solo l’uso della sandbox.

Eventuali commissioni di uno scorer esterno o di un provider MCP vanno direttamente a quel provider. Non fanno parte del limite di spesa dell'esecuzione.

## Chiamate ai modelli dal tuo codice

Il codice del tuo repository e il tuo scorer possono chiamare un modello tramite il gateway dei modelli di Skynet. Leggi [Come funziona l'ottimizzazione](/docs/concepts/optimization).

- Una chiamata con le chiavi di Skynet viene misurata sul budget dell'esecuzione, come qualsiasi altra chiamata al modello. Si applica il ricarico sull'utilizzo.
- Una chiamata con la tua chiave (BYOK) va alla chiave del tuo provider. Skynet non applica costi per il modello.

## Uso della sandbox

Il codice del tuo repository e il tuo scorer girano in una sandbox. Skynet misura il calcolo della sandbox:

1. Prima che una sandbox parta, Skynet riserva un importo dal limite dell'esecuzione.
2. Quando la sandbox termina, Skynet addebita l'uso finale riportato dal provider della sandbox.
3. Il ricarico sull'utilizzo si applica al calcolo della sandbox.

Il calcolo della sandbox fa parte del limite di spesa dell'esecuzione, anche quando le chiamate ai modelli usano la tua chiave.

## Serving

Ogni chiamata all'endpoint di serving di un'esecuzione terminata viene fatturata a parte. Chi chiama paga con il proprio saldo. Imposta `max_cost_cents` nella richiesta per limitare una chiamata. Leggi [Risultati](/docs/guides/results).

## Piani

Ci sono due piani: Free e Pro. Pro alza alcuni limiti, come le esecuzioni contemporanee e lo spazio di archiviazione. Se il rinnovo di Pro non va a buon fine, l'account mantiene i limiti Pro per 7 giorni. Dopo, usa i limiti Free finché il pagamento non va a buon fine. Vedi [Limiti](/docs/limits).

## Tutele della piattaforma

- Un turno interattivo del modello, come una chat con l'assistente, richiede un saldo di almeno 0,05 $ prima di partire.
- L'assistente della procedura guidata è gratuito. Ogni account può avviare 50 turni dell'assistente della procedura guidata per giorno UTC.

## Pagine correlate

- [Esecuzioni](/docs/concepts/runs)
- [Avviare un'esecuzione con la procedura guidata](/docs/guides/submit)
- [Limiti](/docs/limits)
