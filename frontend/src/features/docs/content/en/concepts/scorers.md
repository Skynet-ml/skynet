The scorer tells Skynet how good one version of your repository is. It is a Python function in `scorer.py`. Every engine calls the same function. This page gives the contract, the return value, and examples.

## The contract

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| Argument    | What it holds                                                                    |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | The folder of one version of the repository, after the setup command ran         |
| `case`      | A dict. `case["input"]` holds the input fields. `case["expected"]` holds the expected value, or `None` |

If your function takes only one argument, Skynet calls `score(repo_path)`. Use this form when the scorer runs its own tests and needs no cases.

The scorer runs in the sandbox, in the folder of the version. It can import code from the repository, run a command, or call `serve()` from `serve.py`. Read [How optimization works](/docs/concepts/optimization).

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

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` is a dict keyed by the name of your Output column. Read [Datasets and splits](/docs/concepts/datasets).

### Run the tests of the repository

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### Model as judge

The scorer can call a model. Import `llm` from the `skynet` helper module. The call goes through Skynet's model gateway to the scorer model of the run.

```python tab="scorer.py"
import importlib.util

from skynet import llm


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.serve(case["input"])
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {answer}",
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
