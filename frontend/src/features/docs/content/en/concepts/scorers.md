The scorer tells Skynet how good one version of your repository is. It is a Python function in `scorer.py`. Every engine calls the same function. This page gives the contract, the return value, and examples.

## The contract

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argument    | What it holds                                                                    |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | A folder that holds only your metric files. It holds no other repository file and no version code |
| `case`      | A dict. `case["input"]` holds the input fields. `case["expected"]` holds the expected value, or `None` |
| `output`    | What the `[run]` entry returned for the case                                     |

The `optimize.toml` file of the repository must have a `[run]` section or a `[target.<name>]` stage. The `[run]` `entry` runs one version on one case, in its own sandbox box. Skynet refuses a file with neither, because every version would get the same score.

- A function entry, for example `entry = "serve.py:serve"`, gets `case["input"]` as its argument. `output` is its return value.
- A command entry, for example `entry = ["python", "-m", "pytest", "-q"]`, gets the input as JSON on stdin. `output` is `{"exit_code": ..., "stdout": "..."}`.

The scorer runs in a separate sandbox box. That box holds only your metric files. The scorer cannot import or run the code of the version. It sees the version only through `output`. Read [How optimization works](/docs/concepts/optimization).

## The return value

`score` must return a score with feedback. These shapes are valid:

1. A pair: a number, and a feedback string or a dict with a `"feedback"` key.
2. A dict with a number under `"score"` and a string under `"feedback"`.

The feedback must be a string that is not empty.

The side info can also have a `"scores"` key. It maps names to named scores. Each named score has its own score and feedback, for example `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

The side info can also have a `"cases"` key with the same shape. Each entry is the result of one case. Every engine uses each case as its own objective.

If the return value has a different shape, the check fails and shows the reason.

| Return value                                                                    | Valid                                  |
| ------------------------------------------------------------------------------- | -------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | Yes                                    |
| `(0.8, "Close. Missing the date.")`                                             | Yes                                    |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | Yes                         |
| `0.8`                                                                           | No. There is no feedback.              |
| `(0.8, {})`                                                                     | No. There is no feedback string.       |
| `("0.8", {"feedback": "OK"})`                                                   | No. The score is a string.             |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | No. A named score is not a number.    |

## Write useful feedback

GEPA and Auto read the feedback text to write the next version. Say what is wrong and what a correct output looks like.

| Weak feedback | Useful feedback                                                               |
| ------------- | ----------------------------------------------------------------------------- |
| "Wrong."      | "Expected 'billing', got 'other'. The message mentions a charge."             |
| "Bad format." | "The output is not valid JSON. It has a trailing comma after the last field." |

> [!NOTE]
> The coding agent never sees your scorer source, the expected values or the run secrets. It sees only the scores and the feedback text.

## Examples

### Exact match

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` is a dict keyed by the name of your Output column. Read [Datasets and splits](/docs/concepts/datasets).

### Run the tests of the repository

The `[run]` entry runs the tests. The scorer reads the exit code and the test output.

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output is None:
        return 0.0, {"feedback": "The tests did not run."}
    if output["exit_code"] == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": output["stdout"][-2000:]}
```

### Model as judge

The scorer can call a model. Import `llm` from the `skynet` helper module. The call goes through Skynet's model gateway to the scorer model of the run.

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from skynet import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

The scorer can also use any OpenAI-compatible client with the `OPENAI_BASE_URL`, `APP_API_KEY` and `APP_MODEL` environment variables. The gateway accepts only the scorer model of the run.

## Model calls and secrets

- Calls on Skynet's keys are metered against the run budget. With your own key (BYOK), the call goes to your provider key, with no Skynet model fee. Read [Billing](/docs/concepts/billing).
- Run secrets reach the scorer as environment variables. The variable name is the secret name, for example `os.environ["SEARCH_API_KEY"]`.
- Skynet hides secret values in the scorer output and in the logs.

## Limits

| Item           | Value                              |
| -------------- | ---------------------------------- |
| Scorer timeout | 60 seconds by default, at most 600 |

## Related pages

- [Datasets and splits](/docs/concepts/datasets)
- [Engines](/docs/concepts/engines)
- [Submit a run](/docs/guides/submit)
