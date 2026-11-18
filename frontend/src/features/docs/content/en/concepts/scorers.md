The scorer tells Skynet how good one version of your repository is. It is a Python function in `scorer.py`. Every engine calls the same function. This page gives the contract, the return value, and examples.

## The contract

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Argument    | What it holds                                                                    |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | The folder of one version of the repository, after the setup command ran         |
| `case`      | A dict. `case["input"]` holds the input fields. `case["expected"]` holds the expected value, or `None` |

If your function takes only one argument, Skynet calls `score(repo_path)`. Use this form when the scorer runs its own tests and needs no cases.

The scorer runs in the sandbox, in the folder of the version. It can import code from the repository, run a command, or call `serve()` from `serve.py`. Read [How optimization works](/docs/concepts/optimization).

## The return value

`score` returns one object:

```python
{
    "score": 0.78,
    "feedback": "Misses short names.",
    "scores": {
        "precision": {"score": 0.90},
        "recall": {"score": 0.69, "feedback": "Misses names under 4 letters."},
    },
}
```

- `"score"` is required. It is a finite number. Only this score ranks the versions.
- `"feedback"` is optional text. It is recommended, because GEPA and Auto learn from it. Without feedback, the engines see only the scores, and the run log says so one time.
- `"scores"` is optional. It maps names to named scores. Each named score is an object with its own `"score"` and an optional `"feedback"`. Every engine uses each named score as its own objective.

Any other form or key is an error. The check shows the wrong part and the valid form.

| Return value | Valid |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | Yes |
| `{"score": 0.8}` | Yes. There is no feedback. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | Yes |
| `0.8` | No. The value is not an object. |
| `(0.8, "Close. Missing the date.")` | No. The value is not an object. |
| `{"score": "0.8"}` | No. The score is a string. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | No. A named score must be an object. |
| `{"score": 0.8, "feedbak": "OK"}` | No. The key `feedbak` is unknown. |

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
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` is a dict keyed by the name of your Output column. Read [Datasets and splits](/docs/concepts/datasets).

### Run the tests of the repository

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
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
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
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
