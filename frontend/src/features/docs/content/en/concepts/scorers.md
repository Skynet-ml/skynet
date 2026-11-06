The metric tells Skynet how good an output is. It is a Python function in `metric.py`. Every engine calls the same function. This page gives the contract, the return value, and examples.

## The contract

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argument   | What it holds                                                    |
| ---------- | ---------------------------------------------------------------- |
| `input`    | A dict with the input fields of the case                         |
| `output`   | What your `run(artifact, input)` returned for the case           |
| `expected` | The expected value of the case, or `None` when the case has none |

If your function takes only two arguments, Skynet calls `score(input, output)`.

For a DSPy program, Skynet adapts the DSPy metric call to this same function. You write one metric for every artifact kind. Read [How optimization works](/docs/concepts/optimization).

## The return value

`score` must return a pair (a tuple or a list of two items):

1. **The score.** A number. The optimizer tries to make it higher.
2. **The side info.** A dict. It must have a `"feedback"` key with a string value.

The side info can also have a `"scores"` key. It maps names to numbers, for example `{"format": 1.0, "accuracy": 0.5}`.

If the return value has a different shape, the contract check fails at **Metric return value**.

| Return value                                           | Valid                              |
| ------------------------------------------------------ | ---------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Yes                                |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Yes                                |
| `0.8`                                                  | No. There is no side info.         |
| `(0.8, "Close.")`                                      | No. The side info is not a dict.   |
| `(0.8, {})`                                            | No. There is no feedback string.   |
| `("0.8", {"feedback": "OK"})`                          | No. The score is a string.         |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | No. A named score is not a number. |

## Write useful feedback

GEPA, DSPy GEPA and Auto read the feedback text to write the next version. Say what is wrong and what a correct output looks like.

| Weak feedback | Useful feedback                                                               |
| ------------- | ----------------------------------------------------------------------------- |
| "Wrong."      | "Expected 'billing', got 'other'. The message mentions a charge."             |
| "Bad format." | "The output is not valid JSON. It has a trailing comma after the last field." |

> [!NOTE]
> The optimizer never sees your metric source. With a DSPy optimizer, the reflection model sees only the feedback text. Your expected values stay with the metric.

## Examples

### Exact match

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` is a dict keyed by the name of your Output column. Read [Datasets and splits](/docs/concepts/datasets).

### Several checks

```python tab="metric.py"
import json


def score(input, output, expected=None):
    try:
        data = json.loads(output)
    except ValueError as exc:
        return 0.0, {"feedback": f"Output is not valid JSON: {exc}", "scores": {"format": 0.0, "fields": 0.0}}
    want = expected["fields"]
    found = [key for key in want if key in data]
    fields = len(found) / len(want) if want else 1.0
    missing = [key for key in want if key not in data]
    feedback = "All fields present." if not missing else f"Missing fields: {', '.join(missing)}."
    return 0.5 + 0.5 * fields, {"feedback": feedback, "scores": {"format": 1.0, "fields": fields}}
```

### Model as judge

The metric can call a model. Import `llm` from the `skynet` helper module. A call from the metric goes to the metric model.

```python tab="metric.py"
from skynet import llm


def score(input, output, expected=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {input['question']}\nAnswer: {output}",
        model="openai/gpt-4.1-mini",
        temperature=0,
        max_tokens=512,
        reasoning_effort="low",
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

The metric sets its model in its own `llm()` call. All four arguments shown are optional. Without `model=`, the metric uses the reflection model.

## Accuracy, speed and cost

A metric can blend accuracy with the measured run time and the model cost. Define `REWARD_SPEC` at module level and return the value of `reward`.

```python tab="metric.py"
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.7, "speed": 0.2, "cost": 0.1},
    "accuracy": {},
    "speed": {"reference": 2000},
    "cost": {"reference": 0.01},
}


def score(input, output, expected=None):
    ok = output == expected["label"]
    return reward(REWARD_SPEC, 1.0 if ok else 0.0, "Correct." if ok else "Wrong label.")
```

- `weights` must have `accuracy`, `speed` and `cost`. Each weight is 0 or more, and not all can be 0. Skynet scales them to add up to 1.
- `speed.reference` is in milliseconds. `cost.reference` is in US dollars. Both must be positive.
- Without `accuracy.reference`, accuracy is a score from 0 to 1. With it, accuracy is an error, and lower is better.

Skynet measures the time and the cost itself. The run code cannot change them. If Skynet cannot measure the time or the cost, that part scores 0 and the feedback says so.

The metric can also read the measurements directly with `observation()` and `service_cost()` from `skynet`.

## Limits

| Item                   | Value                              |
| ---------------------- | ---------------------------------- |
| Metric timeout         | 60 seconds by default, at most 600 |
| Contract check samples | 3 cases                            |

## Related pages

- [Datasets and splits](/docs/concepts/datasets)
- [Engines](/docs/concepts/engines)
- [Submit a run](/docs/guides/submit)
