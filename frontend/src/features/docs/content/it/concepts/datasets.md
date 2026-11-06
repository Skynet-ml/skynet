Un dataset è una tabella di casi. Ogni riga è un caso. Skynet assegna un punteggio a ogni versione del tuo repository sui casi con il tuo scorer e usa i punteggi per confrontare le versioni. Questa pagina spiega come Skynet legge i casi, a che cosa servono i ruoli delle colonne e come suddivide i casi in set di addestramento, convalida e test.

## Caricare i dati

Nel passaggio **Input** della procedura guidata hai due opzioni:

- **Carica un file CSV, JSON o Excel**. Skynet accetta file `.csv`, `.json`, `.xlsx` e `.xls`.
- **Scegli dalla libreria**. Usa un dataset che hai salvato in precedenza.

Un'esecuzione può avere al massimo 200.000 casi.

Leggi [Dataset](/docs/guides/datasets) per gestire i dataset salvati.

## Ruoli delle colonne

In **Come leggere i dati**, assegna un ruolo a ogni colonna:

| Ruolo  | Che cosa fa Skynet con la colonna                                     |
| ------ | --------------------------------------------------------------------- |
| Input  | Mette il valore in `case["input"]`, che il tuo scorer riceve    |
| Output | Mette il valore in `case["expected"]`, che il tuo scorer riceve              |
| Salta  | Non usa la colonna                                                    |

Ogni colonna ha anche un tipo: **Testo** o **Immagine**.

Solo una colonna può essere la colonna Output. Se imposti una seconda colonna su Output, la prima passa a Salta.

### Che cosa riceve il tuo codice

Per ogni riga, Skynet costruisce un caso:

- `input` è un dict. Ha una chiave per ogni colonna Input.
- `expected` è un dict con una sola chiave: il nome della colonna Output.

Per questa tabella:

| message             | label   | id  |
| ------------------- | ------- | --- |
| I was charged twice | billing | 17  |

con i ruoli `message` = Input, `label` = Output e `id` = Salta, il tuo codice riceve:

```python tab="input"
{"message": "I was charged twice"}
```

```python tab="expected"
{"label": "billing"}
```

> [!TIP]
> Nel tuo scorer, leggi il valore atteso tramite il nome della colonna, ad esempio `case["expected"]["label"]`.

## Modalità dei dati

I ruoli determinano la modalità dei dati dell'esecuzione:

| Modalità | Quando                       | Che cosa succede                                                                                         |
| -------- | ---------------------------- | -------------------------------------------------------------------------------------------------------- |
| Coppie   | Una colonna è Output         | Ogni riga è un caso con un input e un valore atteso. Servono almeno 2 righe.                             |
| Singolo  | Nessuna colonna è Output     | L'esecuzione usa solo le colonne Input della prima riga. È un unico caso senza valore atteso.            |

In modalità singola, Skynet usa l'unico caso per l'addestramento, la selezione e il report. Nulla viene tenuto da parte.

## La suddivisione

In modalità coppie, Skynet suddivide i casi in tre set:

| Set               | A che cosa serve                                                   |
| ----------------- | ------------------------------------------------------------------ |
| Train             | Il motore propone e prova le versioni su questi casi.              |
| Val (convalida)   | Skynet seleziona la versione migliore su questi casi.              |
| Test              | Skynet riporta il punteggio finale su questi casi.                 |

> [!NOTE]
> I casi di test non raggiungono mai un motore, un proponente o il controllo del contratto. Il punteggio finale si basa su casi che l'ottimizzatore non ha visto.

### La suddivisione predefinita

| Numero di coppie | Train | Val | Test |
| ---------------- | ----- | --- | ---- |
| 5 o più          | 70%   | 15% | 15%  |
| Meno di 5        | 85%   | 15% | 0%   |

Con meno di 5 coppie, Skynet non tiene un set di test.

### Come Skynet conta i casi

Skynet trasforma le frazioni in numeri interi di casi con il metodo dei resti più grandi. I conteggi danno sempre come somma il numero di casi. Un set con una frazione maggiore di zero riceve almeno un caso. Un set con una frazione pari a zero non riceve casi.

Prima della suddivisione, Skynet mescola i casi. Il mescolamento usa il seed dell'esecuzione. Se non fornisci un seed, Skynet ne ricava uno dai casi, dall'obiettivo e dall'artefatto. Così, se invii di nuovo la stessa esecuzione, ottieni la stessa suddivisione.

### Soluzioni di ripiego

- Con meno di 5 coppie, Skynet rimuove il set di test e ridimensiona train e val in modo che la loro somma sia 1.
- Se train è vuoto, Skynet usa i casi di val come train.
- Se non c'è un set val, Skynet seleziona sui casi di train.
- Se non c'è un set di test, Skynet riporta su val. Se non c'è nemmeno un set val, riporta su train.

> [!WARNING]
> Un punteggio che Skynet riporta su train o val non è un punteggio su dati tenuti da parte. Usa almeno 5 coppie per avere un set di test.

## Modificare la suddivisione

La sezione della suddivisione mostra **Split consigliato**. Ha due modalità:

1. **Usa il consiglio**: Skynet usa la suddivisione predefinita descritta sopra.
2. **Selezione manuale**: imposti tu **Train**, **Val** e **Test**.

Nella selezione manuale, ogni campo accetta una frazione da 0 a 1, a passi di 0,05. Ogni campo mostra il numero di casi che riceve. Le tre frazioni devono dare come somma 1. In caso contrario, la procedura guidata mostra «La suddivisione deve sommare a 1.»

## Campioni del contratto

Il controllo del contratto nel passaggio **Metrica ed esecuzione** esegue il tuo setup e il tuo scorer sulla versione iniziale, su 3 casi di esempio. Skynet prende questi campioni solo dai casi di train e val, mai da test.

## Pagine correlate

- [Dataset](/docs/guides/datasets)
- [Metriche e scorer](/docs/concepts/scorers)
- [Avviare un'esecuzione](/docs/guides/submit)
- [Limiti](/docs/limits)
