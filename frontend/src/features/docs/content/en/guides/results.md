The run page shows the progress and the results of a run. This guide explains the tabs on the run page, the scores, and how to export or serve the best version.

## Tabs on the run page

The run page shows only the tabs that apply to the run. Some tabs depend on the path, the status and your experience level.

| Tab                | When it shows                                                 | What it contains                                                                 |
| ------------------ | ------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| **Overview**       | Always                                                        | Progress, scores and the score history                                           |
| **Usage**          | A successful run with a program that Skynet can serve         | A form to run the optimized program, and integration code                        |
| **Artifact** | A repository run with a result | The versions of your artifact, with the best version |
| **Data**           | A finished DSPy program run                                   | The data split into train, validation and test sets, and the test set evaluation |
| **Code**           | When the run has code to show                                 | The run code and the metric                                                      |
| **Reward** | A repository run whose metric declares reward parts | The reward parts and their weights |
| **Artifact** | A finished DSPy program run | The optimized output and **Export results** |
| **Logs**           | Most runs                                                     | The run log                                                                      |
| **Usage and cost** | When you are signed in                                        | What the run spent                                                               |
| **Scheduled run** | A successful DSPy program run | A schedule to run the optimization again |
| **Config**         | Always                                                        | The run configuration                                                            |

> [!NOTE]
> At the **Guided** level, the run page hides the **Data**, **Code**, **Logs** and **Config** tabs. A direct link to one of these tabs still opens it. Change the level in **Settings → Account**.

## Scores

Skynet selects versions on the validation cases. It reports the final score on the test cases. The engines never see the test cases. Read [Datasets and splits](/docs/concepts/datasets).

The **Overview** tab shows:

- **Improvement**: the change from the starting version to the best version.
- **Score progression**: the score over the run.
- **Score breakdown**: each metric, with the **Baseline**, **Optimized** and **Change** columns.

While the run is active, the overview also shows **Elapsed**, **Remaining** and **Rate**.

For a repository run, the overview also shows **Named scores** when your metric reports named scores.

For a DSPy program run, the **Data** tab has a **Test set evaluation**. It shows the baseline and optimized score for each test case.

### When the best version is the starting version

Sometimes no candidate beats the starting version. Skynet then returns the starting version. The **Artifact** tab tells you that a regression guard was applied.

If the run stopped at its budget, the run page tells you which version Skynet saved. Read [Runs](/docs/concepts/runs).

## Export the best version

### Repository runs

The **Artifact** tab lists the **Versions** of your artifact. For each version you can:

- Copy the version.
- Download the version as a file.
- For a repository run, download the version as a zip of the whole repository.

You can compare a version with another version. The tab also shows the **Scorer feedback** for a version.

### DSPy program runs

The **Artifact** tab has an **Export results** panel. Open the **Download** menu. It shows the formats that the run has:

| Option            | Format                                           |
| ----------------- | ------------------------------------------------ |
| DSPy program      | `.zip`, a self-contained export that you can run |
| Optimized program | `.pkl`                                           |
| Prompt            | `.json`                                          |
| Optimized code    | `.py`                                            |
| Logs              | `.csv`                                           |

## Serve the best version

You can call an optimized DSPy program from your own code. The **Usage** tab has two parts:

- A form. Enter the input fields and select **Run** to get a prediction.
- **Integration code** in cURL, Python, JavaScript, Go and DSPy.

The program always runs on the model settings that it was optimized with. You must give all input fields. Skynet ignores extra fields.

To call the program, create an API token in **Settings → API**. Then send a request:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"question": "What is DSPy?"}}'
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
    json={"inputs": {"question": "What is DSPy?"}},
)
print(response.json()["outputs"])
```

Replace `question` with the input fields of your program. The **Integration code** on the **Usage** tab fills in the real URL and field names for you.

The response contains `outputs`, the `input_fields`, the `output_fields` and the `model_used`.

> [!TIP]
> The request body also accepts `max_cost_cents`. It sets the most this one call may cost, in cents. Some runs require it.

To get partial outputs as they arrive, send the same request to `/serve/<optimization_id>/stream`. The response is a stream of server-sent events.

Read [API](/docs/api) for authentication and the other endpoints.

## Share the results

You can share a run with other people, or list it in the public Explore gallery. Read [Share and publish](/docs/guides/sharing).
