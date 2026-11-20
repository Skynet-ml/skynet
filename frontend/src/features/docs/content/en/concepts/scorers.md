The scorer tells Skynet how good one version of your repository is. It is a Python function in `scorer.py`. Every engine calls the same function. This page gives the contract, the return value, and examples.

## The contract

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Argument    | What it holds                                                                    |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | A folder that holds only your metric files. It holds no other repository file and no version code |
| `case`      | A dict. `case["input"]` holds the input fields. `case["expected"]` holds the expected value, or `None` |
| `output`    | What the `[run]` entry returned for the case, or `None` when the file has no `[run]` |

The `ascent.toml` file of the repository must have a `[run]` section or a `[target.<name>]` stage. The `[run]` `entry` runs one version on one case, in its own sandbox box. Skynet refuses a file with neither, because every version would get the same score.

- A function entry, for example `entry = "serve.py:serve"`, gets `case["input"]` as its argument. `output` is its return value.
- A command entry, for example `entry = ["python", "run_tests.py"]`, reads `{"input": ...}` from the JSON file at the path in `ASCENT_INPUT`. It writes `{"output": ...}` to the path in `ASCENT_OUTPUT`. `output` is the value under `"output"`. What the command prints goes to the logs only.

The scorer runs in a separate sandbox box. That box holds only your metric files. The scorer cannot import or run the code of the version. It sees the version only through `output`, or through `call()` to a `[target.<name>]` stage. Import `call` with `from openascent import call`. Read [How optimization works](/docs/concepts/optimization).

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

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` is a dict keyed by the name of your Output column. Read [Datasets and splits](/docs/concepts/datasets).

### Run the tests of the repository

The `[run]` entry runs the tests. `pytest` exits with a nonzero code when a test fails, and a nonzero exit code fails the case. Thus the wrapper `run_tests.py` writes the result to `ASCENT_OUTPUT` and exits 0. The scorer reads that result.

```toml tab="ascent.toml"
version = 1

[run]
entry = ["python", "run_tests.py"]
timeout = 300
```

```python tab="run_tests.py"
import json
import os
import subprocess

result = subprocess.run(["python", "-m", "pytest", "-q"], capture_output=True, text=True)
with open(os.environ["ASCENT_OUTPUT"], "w") as f:
    json.dump({"output": {"passed": result.returncode == 0, "log": result.stdout[-2000:]}}, f)
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["passed"]:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": output["log"]}
```

When the `[run]` entry fails on a case, that case has no score. If the entry cannot start, times out or a signal stops it, or if a function entry raises or exits before it returns, the feedback is `Run failed: …`, and Skynet does not call the scorer. A command entry also fails when it exits with a nonzero code or writes no valid output file. A version with such a case has no score. It cannot be the best version, reaches no `[outcome] target` and opens no pull request. If a failure must count as a number, catch the failure in your program, write an output, and let the scorer score it. Read the [repository rules](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md).

### Model as judge

The scorer can call a model. Import `llm` from the `openascent` helper module. The call goes through Skynet's model gateway to the scorer model of the run.

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from openascent import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

The scorer can also use any OpenAI-compatible client with the `OPENAI_BASE_URL`, `APP_API_KEY` and `APP_MODEL` environment variables. The gateway accepts only the scorer model of the run.

## Model calls and secrets

- Calls on Skynet's keys are metered against the run budget. With your own key (BYOK), the call goes to your provider key, with no Skynet model fee. Read [Billing](/docs/concepts/billing).
- The scorer gets no run secrets as environment variables. A secret reaches the scorer only through a `[services.<name>]` table that `[metric] services` lists, or through a server in the `[metric] mcp` file. Write the secret in the table's `headers` as `${NAME}`. The scorer sends its requests to the URL in `ASCENT_SERVICE_<NAME>_URL`. Skynet adds the secret outside the sandbox.
- Skynet hides secret values in the scorer output and in the logs.

## Limits

| Item           | Value                              |
| -------------- | ---------------------------------- |
| Scorer timeout | 60 seconds by default, at most 600 |

## Related pages

- [Datasets and splits](/docs/concepts/datasets)
- [Engines](/docs/concepts/engines)
- [Submit a run](/docs/guides/submit)
