Skynet is Optimize Anything: one product that improves a DSPy program or a repository against your metric. The artifact kind decides which engines can work on it.

## The shared contract

Every run has two files. `run.py` runs the artifact on one case. `metric.py` scores the output.

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

- `artifact` is the current version that the optimizer proposes.
- `input` is a dict that holds the input fields of one case.
- `expected` is the expected value of the case, or `None` when the case has none.
- `score` returns a pair: a number, and a dict with a `"feedback"` string.

The `skynet` helper module gives `llm`, `Image` and `log`. A call to `llm` from the run code goes to the task model. A call to `llm` from the metric goes to the metric model.

> [!NOTE]
> The run code runs in its own process in the sandbox. It cannot see the metric source, the expected value or the metric key.

Read [Metrics and scorers](/docs/concepts/scorers) for the full metric contract.

## Artifact kinds and engines

| Artifact kind | Engines                                                                      |
| ------------- | ---------------------------------------------------------------------------- |
| Repository | GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve |
| DSPy program  | Auto, DSPy GEPA, DSPy MIPROv2, DSPy BootstrapFewShot                         |

The engines come in two families:

- The **native engines** treat the artifact as a black box. They work on repositories.
- The **DSPy optimizers** work inside a DSPy program. They work only on DSPy program artifacts.

DSPy MIPROv2 and DSPy BootstrapFewShot need pairs (cases with expected outputs). With a single input, a DSPy program can use only Auto and DSPy GEPA.

## Repositories

The native engines change the files in the paths that you allow. Your metric scores each version of the repository.

The six native engines use different search methods:

- **GEPA** evolves versions by reflection and keeps a Pareto front.
- **Best-of-N** asks the reflection model for independent proposals and keeps the best one.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** and **ShinkaEvolve** use a coding agent to propose versions.

Read [Engines](/docs/concepts/engines) for each engine and its settings.

## DSPy program artifacts

A DSPy program is one artifact kind. Your `run.py` builds a `dspy.Module` at module level and calls it inside `run`. If the file has more than one module, name the one to optimize `program`.

What happens when a DSPy optimizer runs:

1. Skynet finds your module and wraps your `run` function around it. Every case goes through your own `run`.
2. The DSPy optimizer compiles the program on the train cases.
3. It selects on the validation cases.
4. The winner is the instruction text of each predictor, plus the full compiled state of the module. The state includes the few-shot demos.

The final scores measure the same program that Skynet saves.

| Optimizer             | Reads feedback text |
| --------------------- | ------------------- |
| DSPy GEPA             | Yes                 |
| DSPy MIPROv2          | No, score only      |
| DSPy BootstrapFewShot | No, score only      |

DSPy BootstrapFewShot does not use a reflection model.

> [!TIP]
> The DSPy reflection model sees only the feedback text that your metric returns. It never sees the metric source.

## Auto

**Auto** is the default choice in the wizard.

- **Repository**: Auto is not available. Select one native engine.
- On a DSPy program, Auto runs DSPy GEPA.

Auto reads the feedback text from your metric.

## Which engine to pick

| Your situation                                        | Use                                                                |
| ----------------------------------------------------- | ------------------------------------------------------------------ |
| You have code in a GitHub repository | A repository. Select one native engine. |
| You have a DSPy program                               | A DSPy program artifact. Start with Auto.                          |
| You want few-shot demos picked from your data         | A DSPy program artifact with DSPy BootstrapFewShot or DSPy MIPROv2 |

> [!WARNING]
> GEPA, DSPy GEPA and Auto learn from the feedback text. If your feedback is always the same string, these optimizers have less information to work with.

## Related pages

- [Engines](/docs/concepts/engines)
- [Datasets and splits](/docs/concepts/datasets)
- [Metrics and scorers](/docs/concepts/scorers)
- [Submit a run](/docs/guides/submit)
