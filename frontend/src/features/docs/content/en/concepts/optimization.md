Skynet is Optimize Anything: every run optimizes a repository. An engine proposes new versions of the repository, your scorer scores each version, and Skynet keeps the best one.

## The shared contract

Every run has a repository, an `ascent.toml` file with a `[run]` entry or a `[target.<name>]` stage, and a scorer. The `[run]` entry runs one version of the repository on one case. The scorer is a Python function that gets the output of the entry and the case.

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    ok = output == case["expected"]
    return {"score": 1.0 if ok else 0.0, "feedback": "Match." if ok else "No match."}
```

- `output` is what the `[run]` entry returned for the case.
- `case` is a dict. `case["input"]` holds the input fields of one case. `case["expected"]` holds the expected value, or `None` when the case has none.
- `repo_path` is a folder that holds only your metric files.
- `score` returns one object: a number under `"score"`, and an optional `"feedback"` string.

The repository has a `serve.py` file at its root with `def serve(input)`. The `[run]` entry calls it in its own sandbox box. Skynet uses it to serve the best version after the run.

The scorer runs in a separate sandbox box. That box holds only your metric files and no version code, so the scorer cannot import `serve.py`. It sees the version only through `output`, or through `call()` to a `[target.<name>]` stage. Import `call` with `from openascent import call`. Without a `[run]` entry or a `[target.<name>]` stage, Skynet refuses the `ascent.toml` file.

> [!NOTE]
> The coding agent that writes versions cannot see the scorer source, the expected values or the run secrets.

Read [Metrics and scorers](/docs/concepts/scorers) for the full scorer contract.

## Model calls from your code

Your repository code and your scorer can call a model through Skynet's model gateway. The sandbox gives an OpenAI-compatible endpoint in environment variables:

| Variable             | What it holds                                              |
| -------------------- | ---------------------------------------------------------- |
| `OPENAI_BASE_URL`    | The gateway URL. `APP_GATEWAY_URL` holds the same value.   |
| `APP_API_KEY`        | A token for this run only. It is not a provider key.       |
| `APP_MODEL`          | The model name to send. It is the scorer model of the run. |
| `ANTHROPIC_BASE_URL` | The gateway URL for Anthropic SDKs.                        |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["APP_API_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["APP_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- Calls on Skynet's keys are metered against the run budget.
- With your own key (BYOK), the call goes to your provider key. Skynet charges no model fee.
- The gateway refuses requests for any other model.
- Your code and your scorer get no run secrets as environment variables. A secret reaches a role only through a `[services.<name>]` table that the role lists in `[run] services` or `[metric] services`, or through a server in the role's `mcp` file. Write the secret in the table's `headers` as `${NAME}`. Skynet adds the secret outside the sandbox and hides secret values in the output.

The sandbox has no other network access.

## Engines

Every engine works on repositories:

- **Auto** is the default. It tries several engines, then continues GEPA from the best version.
- **GEPA** evolves versions by reflection and keeps a Pareto front.
- **Best-of-N** asks the reflection model for independent proposals and keeps the best one.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** and **ShinkaEvolve** use a coding agent to propose versions.

The engines change only the files in the paths that you allow. Read [Engines](/docs/concepts/engines) for each engine and its settings.

## Auto

**Auto** is the default choice in the wizard, for every run.

1. Auto explores with five engines: GEPA, AutoResearch, Meta-Harness, AutoSaddler and ShinkaEvolve.
2. It picks the best version that they found.
3. It continues GEPA from that version until the budget runs out.

Auto reads the feedback text from your scorer.

## Where the starting version comes from

| Your situation                       | Use                                                                                  |
| ------------------------------------ | ------------------------------------------------------------------------------------ |
| You have code in a GitHub repository | **Use a repository**. The code at the branch or commit that you pick is the start.   |
| You have only a task and data        | **Create a new repository**. Skynet writes a first solution. That is the start.      |

> [!WARNING]
> GEPA and Auto learn from the feedback text. If your feedback is always the same string, these optimizers have less information to work with.

## Related pages

- [Engines](/docs/concepts/engines)
- [Datasets and splits](/docs/concepts/datasets)
- [Metrics and scorers](/docs/concepts/scorers)
- [Submit a run](/docs/guides/submit)
