Skynet è Optimize Anything: un unico prodotto che migliora un programma DSPy o un repository in base alla tua metrica. Il tipo di artefatto decide quali motori possono lavorarci.

## Il contratto comune

Ogni esecuzione ha due file. `run.py` esegue l'artefatto su un caso. `metric.py` assegna un punteggio all'output.

```python tab="run.py"
from skynet import llm


def run(artifact, input):
    return llm(artifact, input=input)
```

```python tab="metric.py"
def score(input, output, expected=None):
    ok = output == expected
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `artifact` è la versione corrente proposta dall'ottimizzatore.
- `input` è un dict che contiene i campi di input di un caso.
- `expected` è il valore atteso del caso, oppure `None` se il caso non ne ha uno.
- `score` restituisce una coppia: un numero e un dict con una stringa `"feedback"`.

Il modulo di supporto `skynet` fornisce `llm`, `Image` e `log`. Una chiamata a `llm` dal codice di esecuzione va al modello dell'attività. Una chiamata a `llm` dalla metrica va al modello della metrica.

> [!NOTE]
> Il codice di esecuzione gira in un proprio processo nella sandbox. Non può vedere il sorgente della metrica, il valore atteso né la chiave della metrica.

Leggi [Metriche e scorer](/docs/concepts/scorers) per il contratto completo della metrica.

## Tipi di artefatto e motori

| Tipo di artefatto | Motori                                                                       |
| ----------------- | ---------------------------------------------------------------------------- |
| Repository | GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve |
| Programma DSPy    | Auto, DSPy GEPA, DSPy MIPROv2, DSPy BootstrapFewShot                         |

I motori si dividono in due famiglie:

- I **motori nativi** trattano l'artefatto come una scatola nera. Lavorano sui repository.
- Gli **ottimizzatori DSPy** lavorano all'interno di un programma DSPy. Lavorano solo sugli artefatti Programma DSPy.

DSPy MIPROv2 e DSPy BootstrapFewShot richiedono coppie (casi con output attesi). Con un singolo input, un programma DSPy può usare solo Auto e DSPy GEPA.

## Repository

I motori nativi modificano i file nei percorsi che consenti. La tua metrica assegna un punteggio a ogni versione del repository.

I sei motori nativi usano metodi di ricerca diversi:

- **GEPA** fa evolvere le versioni tramite riflessione e mantiene un fronte di Pareto.
- **Best-of-N** chiede al modello di riflessione proposte indipendenti e conserva la migliore.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** e **ShinkaEvolve** usano un agente di programmazione per proporre versioni.

Leggi [Motori](/docs/concepts/engines) per ogni motore e le sue impostazioni.

## Artefatti Programma DSPy

Un programma DSPy è un tipo di artefatto. Il tuo `run.py` costruisce un `dspy.Module` a livello di modulo e lo chiama dentro `run`. Se il file contiene più di un modulo, chiama `program` quello da ottimizzare.

Che cosa succede quando gira un ottimizzatore DSPy:

1. Skynet trova il tuo modulo e lo avvolge nella tua funzione `run`. Ogni caso passa per la tua `run`.
2. L'ottimizzatore DSPy compila il programma sui casi di addestramento.
3. Seleziona sui casi di convalida.
4. Il vincitore è il testo delle istruzioni di ogni predictor, più lo stato compilato completo del modulo. Lo stato include le demo few-shot.

I punteggi finali misurano lo stesso programma che Skynet salva.

| Ottimizzatore         | Legge il testo del feedback |
| --------------------- | --------------------------- |
| DSPy GEPA             | Sì                          |
| DSPy MIPROv2          | No, solo il punteggio       |
| DSPy BootstrapFewShot | No, solo il punteggio       |

DSPy BootstrapFewShot non usa un modello di riflessione.

> [!TIP]
> Il modello di riflessione DSPy vede solo il testo del feedback restituito dalla tua metrica. Non vede mai il sorgente della metrica.

## Auto

**Auto** è la scelta predefinita nella procedura guidata.

- **Repository**: Auto non è disponibile. Scegli un motore nativo.
- Su un programma DSPy, Auto esegue DSPy GEPA.

Auto legge il testo di feedback della tua metrica.

## Quale motore scegliere

| La tua situazione                                              | Usa                                                                        |
| -------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Hai codice in un repository GitHub | Un repository. Scegli un motore nativo. |
| Hai un programma DSPy                                          | Un artefatto Programma DSPy. Inizia con Auto.                              |
| Vuoi demo few-shot scelte dai tuoi dati                        | Un artefatto Programma DSPy con DSPy BootstrapFewShot o DSPy MIPROv2       |

> [!WARNING]
> GEPA, DSPy GEPA e Auto imparano dal testo del feedback. Se il tuo feedback è sempre la stessa stringa, questi ottimizzatori hanno meno informazioni su cui lavorare.

## Pagine correlate

- [Motori](/docs/concepts/engines)
- [Dataset e suddivisioni](/docs/concepts/datasets)
- [Metriche e scorer](/docs/concepts/scorers)
- [Avviare un'esecuzione](/docs/guides/submit)
