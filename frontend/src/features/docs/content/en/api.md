The Skynet HTTP API lets scripts and tools do what the web app does: start runs, watch them, and read the results. This page shows how to sign requests, where the full reference is, and one complete example.

> [!TIP]
> Do you use an AI agent such as Claude Code, Codex or Cursor? Connect it through [MCP](/docs/mcp) instead. The agent then starts and watches runs for you, with a spending limit for each run.

## Base URL

Send every request to:

```text
https://api.skynetml.com
```

## Authentication

The API uses API tokens.

1. Open **Settings** in Skynet, then open the **API** tab.
2. Click **Generate token**.
3. Copy the token at once. Skynet shows the full token only one time.

Send the token in the `Authorization` header of every request:

```http
Authorization: Bearer skyd_...
```

- Tokens start with `skyd_`.
- Each user has at most one valid token. A new token stops the old token.
- Click **Revoke token** to stop a token.

> [!WARNING]
> Treat the token like a password. Keep it out of source code and logs. Read [Security](/docs/security) for how Skynet stores tokens.

## Interactive reference

The full reference is at [api.skynetml.com/scalar](https://api.skynetml.com/scalar). It lists each public endpoint, its fields and its responses. You can send test requests from that page.

The reference reads the OpenAPI document at `https://api.skynetml.com/openapi.public.json`. Use that file to generate a client in your language.

## Main endpoints

| Method and path                                 | What it does                                           |
| ----------------------------------------------- | ------------------------------------------------------ |
| `GET /models`                                   | Lists the models you can use.                          |
| `GET /optimizers`                               | Lists the optimizers that fit a setup.                 |
| `GET /blackbox/engines`                         | Lists the native engines and if each one is available. |
| `POST /blackbox/run`                            | Starts a run on a GitHub repository.                   |
| `GET /optimizations`                            | Lists your runs.                                       |
| `GET /optimizations/{optimization_id}`          | Returns one run: status, progress, logs and result.    |
| `GET /optimizations/{optimization_id}/artifact` | Returns the best version that the run found.           |
| `POST /optimizations/{optimization_id}/cancel`  | Stops a run.                                           |
| `POST /serve/{optimization_id}`                 | Runs the best version on a new input.                  |
| `GET /health`                                   | Shows if the service is up.                            |

## Start a run

Skynet is Optimize Anything: every run optimizes a repository. `POST /blackbox/run` takes the same setup as the submit wizard. The example improves a repository that sorts support messages into labels.

```bash tab="curl"
curl https://api.skynetml.com/blackbox/run \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "objective": "Sort each support message into billing, bug or other.",
  "recipe": "repo",
  "target": {
    "kind": "repo",
    "repo": {
      "provider": "github",
      "repository": "your-org/support-labels",
      "branch": "main",
      "editable_paths": ["serve.py", "prompts/"]
    }
  },
  "cases": [
    { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
    { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
    {
      "input": { "message": "Do you have a Hebrew interface?" },
      "expected": { "label": "other" }
    }
  ],
  "input_fields": ["message"],
  "expected_field": "label",
  "scorer": {
    "kind": "python",
    "metric_code": "import importlib.util\n\n\ndef score(repo_path, case):\n    spec = importlib.util.spec_from_file_location(\"serve\", f\"{repo_path}/serve.py\")\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    ok = str(module.serve(case[\"input\"])).strip().lower() == case[\"expected\"][\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n",
    "model": { "name": "<model from GET /models>" }
  },
  "strategy": { "mode": "auto" },
  "budget": { "max_scorer_runs": 200 },
  "reflection_model_config": { "name": "<model from GET /models>" },
  "max_cost_cents": 500
}
```

Important fields:

- `target.repo.repository` is the GitHub repository as `owner/name`. Connect GitHub in **Settings → Connectors** first.
- `target.repo.editable_paths` lists the files and folders that the optimizer may change. `"."` means the whole repository.
- `target.repo.commit` is optional. It is a 40-character SHA. Without it, Skynet uses the latest commit on the branch.
- `target.repo.secrets` is optional. Each secret has a `name` and a `value`. It reaches your code and your scorer as an environment variable.
- `scorer.metric_code` defines `score(repo_path, case)`. `scorer.model` is the model that your code and your scorer can call through the model gateway.
- `strategy.mode` is `auto` (the default) or `single`. With `single`, set `strategy.engine` to one engine name from `GET /blackbox/engines`.
- `max_cost_cents` is the spending limit for this run, in US cents.
- `is_private` is `false` by default. Set it to `true` to keep the run private.

Read [Metrics and scorers](/docs/concepts/scorers) for the rules of `metric_code`.

### Responses

| Status | Meaning                                                                                                                                      |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet queued the run. The body has `optimization_id` and `status`.                                                                          |
| `400`  | The target is not a repository, or the scorer does not load.                                                                                |
| `202`  | The setup check is still running. Wait for the seconds in the `Retry-After` header. Then send the same body with the same `Idempotency-Key`. |
| `402`  | Your balance is too low.                                                                                                                     |
| `409`  | You reached a quota, or the setup check is out of date.                                                                                      |
| `422`  | The request does not fit the optimizer.                                                                                                      |

> [!NOTE]
> Always send an `Idempotency-Key` header. If you send the same request again with the same key, Skynet returns the first run. It does not start a second run.

## Watch a run

Poll the run until its status is final:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

The `status` field is one of:

| Status                             | Final |
| ---------------------------------- | ----- |
| `pending`, `validating`, `running` | No    |
| `paused`                           | No    |
| `success`                          | Yes   |
| `failed`, `cancelled`, `stopped`   | Yes   |

Poll every 10 to 30 seconds. A run can take from minutes to hours.

The [API reference](https://api.skynetml.com/scalar) lists every field of the run response.

## Use the result

When the status is `success`, get the best version:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

To run the best version on a new input, send `POST /serve/{optimization_id}` with an `Idempotency-Key` header. The repository must have a `serve.py` file with `def serve(input)`. Read [Results](/docs/guides/results) for the request, the response and what each score means.

## Limits

The API uses the same quotas, rate limits and billing as the web app. Read [Limits](/docs/limits) and [Billing](/docs/concepts/billing).
