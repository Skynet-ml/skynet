This page shows how to start your first run in about ten minutes. You sign in, upload a small dataset, fill in the four wizard steps, and start the run. In the example, Skynet creates a new repository that sorts support messages into labels, and then improves it.

## Before you start

You need:

- A Skynet account.
- A GitHub account. Skynet creates the repository in this account.
- A CSV, JSON or Excel file (`.csv`, `.json`, `.xlsx` or `.xls`). Each row is one case.
- One or more input columns, for example `message`.
- One expected-output column, for example `label`.

> [!TIP]
> Use at least 5 rows. With fewer than 5 pairs, Skynet keeps no test set, so the final score comes from cases the optimizer used.

## 1. Sign in

Sign in to Skynet. Connect GitHub in **Settings → Connectors**. Then open the submit page at `/submit`.

> [!NOTE]
> This page uses the labels of the **Standard** level. The **Guided** level uses simpler labels, skips the Optimizer step, and selects **Auto** for you. Change the level in Settings.

## 2. Task

The first step is **Task**. It asks "What do you want to optimize?"

1. In **Objective**, write what a good result is. Use plain words, for example "Sort each support message into billing, bug or other."
2. Optional: in **Background**, add context that helps the optimizer.
3. Select **Create a new repository**. Enter a name, for example `support-labels`, and select **Private** or **Public**.

Skynet creates the repository in your GitHub account. The assistant writes a first solution from your task and data. This solution is the starting version. If you already have code, select **Use a repository** instead. Read [Submit a run](/docs/guides/submit).

## 3. Inputs

The second step is **Inputs**.

1. Select **Upload a CSV, JSON or Excel file**, or **Pick from library** to use a saved dataset.
2. Under **How to read the data**, give each column a role:
   - **Input**: your code receives this column.
   - **Output**: the expected value. The scorer receives it.
   - **Skip**: Skynet does not use this column.
3. Keep **Use recommendation** for the split. For most datasets the split is 70% train, 15% validation and 15% test.

Read [Datasets and splits](/docs/concepts/datasets) for the full rules.

## 4. Optimizer

The third step is **Optimizer** ("Optimizer, models and budget").

- Keep the optimizer on **Auto**.
- Select a **Reflection model**. The optimizer uses it to write new versions. This model is required.
- Under **Budget**, keep **Max scorer runs** at its default of 200.

Read [Engines](/docs/concepts/engines) to pick a specific engine.

## 5. Metric & run

The last step is **Metric & run**. The assistant writes the scorer for you. You can edit it. The scorer gets the path of one version of the repository and one case:

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = str(module.serve(case["input"])).strip().lower()
    want = case["expected"]["label"].strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`case["input"]` holds the Input columns of one case. `case["expected"]` holds the Output column, keyed by its column name. The repository has a `serve.py` file with `def serve(input)`. Skynet uses the same function to serve the best version after the run.

Then run the check. The check runs your setup and your scorer on the starting version in the sandbox. You can start the run only after the check passes.

## 6. Start and watch

The summary screen shows "Ready to run". Its tabs are General, Data, Models, Budget and Reward. Check the setup, then start the optimization.

Open the run from your optimizations list to watch it. Skynet pushes each candidate to its own branch in your new repository. The `skynet/best` branch always points at the best version. Read [Results](/docs/guides/results) to understand the scores and the winning version.

## Next steps

- Write a better scorer: [Metrics and scorers](/docs/concepts/scorers).
- Learn how the engines work: [How optimization works](/docs/concepts/optimization).
- Learn what a run costs: [Billing](/docs/concepts/billing).
