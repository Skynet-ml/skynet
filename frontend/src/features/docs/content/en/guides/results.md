The run page shows the progress and the results of a run. This guide explains the tabs on the run page, the scores, and how to export or serve the best version.

## Tabs on the run page

The run page shows only the tabs that apply to the run. Some tabs depend on the status and your experience level.

| Tab                | When it shows                                              | What it contains                                                                 |
| ------------------ | ---------------------------------------------------------- | -------------------------------------------------------------------------------- |
| **Overview**       | Always                                                     | Progress, scores and the score history                                           |
| **Usage**          | A successful run whose repository has a `serve.py` file    | A form to call the best version, and integration code                            |
| **Artifact**       | A run with a result                                        | The versions of your repository, with the best version                           |
| **Data**           | A finished run with cases                                  | The data split into train, validation and test sets                              |
| **Code**           | When the run has code to show                              | The scorer                                                                       |
| **Reward**         | A run whose scorer declares reward parts                   | The reward parts and their weights                                               |
| **Logs**           | Most runs                                                  | The run log                                                                      |
| **Usage and cost** | When you are signed in                                     | What the run spent                                                               |
| **Scheduled run**  | A successful run                                           | A schedule to run the optimization again from the best version                   |
| **Config**         | Always                                                     | The run configuration                                                            |

> [!NOTE]
> At the **Guided** level, the run page hides the **Data**, **Code**, **Logs** and **Config** tabs. A direct link to one of these tabs still opens it. Change the level in **Settings → Account**.

## Scores

Skynet selects versions on the validation cases. It reports the final score on the test cases. The engines never see the test cases. Read [Datasets and splits](/docs/concepts/datasets).

The **Overview** tab shows:

- **Improvement**: the change from the starting version to the best version.
- **Score progression**: the score over the run.
- **Score breakdown**: each score, with the **Baseline**, **Optimized** and **Change** columns.

While the run is active, the overview also shows **Elapsed**, **Remaining** and **Rate**.

The overview also shows **Named scores** when your scorer reports named scores.

### When the best version is the starting version

Sometimes no candidate beats the starting version. Skynet then returns the starting version. The **Artifact** tab tells you that a regression guard was applied.

If the run stopped at its budget, the run page tells you which version Skynet saved. Read [Runs](/docs/concepts/runs).

## Export the best version

The **Artifact** tab lists the **Versions** of your repository. For each version you can:

- Copy the version.
- Download the version as a file.
- Download the version as a zip of the whole repository.

You can compare a version with another version. The tab also shows the **Scorer feedback** for a version.

### Branches in a new repository

When Skynet created the repository for the run, it pushes each candidate to its own branch, `skynet/candidate-<version id>`. The `skynet/best` branch always points at the best version. It moves when a better version appears.

## Serve the best version

You can call the best version from your own code. The repository must have a `serve.py` file at its root that defines `serve(input)`:

```python tab="serve.py"
def serve(input):
    return {"label": classify(input["message"])}
```

- `input` is the dict of input fields from the request.
- The return value must be JSON-serializable. A dict becomes `outputs`. Any other value becomes `{"output": value}`.
- `serve.py` can call a model with `llm` from the `skynet` helper module. It does not get the run's secrets.

Skynet runs the best version in the same protected sandbox as the run. The **Usage** tab has two parts:

- A form. Enter the input fields and select **Run** to get a result.
- **Integration code** in cURL, Python, JavaScript and Go.

To call the best version, create an API token in **Settings → API**. Then send a request. The `Idempotency-Key` header is required:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5}'
```

```python tab="Python"
import os
import uuid
import requests

response = requests.post(
    f"{os.environ['API_URL']}/serve/<optimization_id>",
    headers={
        "Authorization": f"Bearer {os.environ['APP_API_TOKEN']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5},
)
print(response.json()["outputs"])
```

Replace `message` with the input fields of your run. The **Integration code** on the **Usage** tab fills in the real URL and field names for you.

The response contains `outputs`, the `input_fields`, the `output_fields`, the `model_used`, `cents_charged` and `budget`.

> [!TIP]
> Each call is billed on its own, from the balance of the caller. `max_cost_cents` sets the most this one call may cost, in cents. If you send the same `Idempotency-Key` again, Skynet does not charge the call again.

Read [API](/docs/api) for authentication and the other endpoints.

## Share the results

You can share a run with other people, or list it in the public Explore gallery. Read [Share and publish](/docs/guides/sharing).
