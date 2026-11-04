This page shows how to start your first run in about ten minutes. You sign in, upload a small dataset, fill in the four wizard steps, and start the run. The example improves a DSPy program that sorts support messages into labels.

## Before you start

You need:

- A Skynet account.
- A CSV, JSON or Excel file (`.csv`, `.json`, `.xlsx` or `.xls`). Each row is one case.
- One or more input columns, for example `message`.
- One expected-output column, for example `label`.

> [!TIP]
> Use at least 5 rows. With fewer than 5 pairs, Skynet keeps no test set, so the final score comes from cases the optimizer used.

## 1. Sign in

Sign in to Skynet. Then open the submit page at `/submit`.

> [!NOTE]
> This page uses the labels of the **Standard** level. The **Guided** level uses simpler labels, skips the Optimizer step, and selects **Auto** for you. Change the level in Settings.

## 2. Task

The first step is **Task**. It asks "What do you want to optimize?"

1. In **Objective**, write what a good result is. Use plain words, for example "Sort each support message into billing, bug or other."
2. Optional: in **Background**, add context that helps the optimizer.
3. In **What gets optimized**, select **DSPy program**.
4. In **Starting version**, paste the current instruction of your program.

## 3. Inputs

The second step is **Inputs**.

1. Select **Upload a CSV, JSON or Excel file**, or **Pick from library** to use a saved dataset.
2. Under **How to read the data**, give each column a role:
   - **Input**: the run code receives this column.
   - **Output**: the expected value. The metric receives it.
   - **Skip**: Skynet does not use this column.
3. Keep **Use recommendation** for the split. For most datasets the split is 70% train, 15% validation and 15% test.

Read [Datasets and splits](/docs/concepts/datasets) for the full rules.

## 4. Optimizer

The third step is **Optimizer** ("Optimizer, models and budget").

- Keep the optimizer on **Auto**.
- Select a **Reflection model**. The optimizer uses it to write new versions. This model is required.
- Select a **Task model**. Your program uses it. A DSPy program must have one.
- Under **Budget**, keep **Max scorer runs** at its default of 200.

Read [Engines](/docs/concepts/engines) to pick a specific engine.

## 5. Metric & run

The last step is **Metric & run** ("Code and metric"). You write two files.

```python tab="run.py"
import dspy

program = dspy.Predict("message -> label")


def run(artifact, input):
    return program(message=input["message"]).label
```

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"].strip().lower()
    got = str(output).strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`artifact` is the current instruction that the optimizer proposes. Skynet applies it to `program`. `input` holds the Input columns of one case. `expected` holds the Output column, keyed by its column name.

Then select **Check** under **Contract check**. The check runs your code and your metric in the sandbox on 3 sample cases. It checks:

| Check               | What fails                                                        |
| ------------------- | ----------------------------------------------------------------- |
| Run code signature  | `run.py` does not define `run(artifact, input)`                   |
| Metric signature    | `metric.py` does not define `score(input, output, expected=None)` |
| Metric model        | The metric model is not usable                                    |
| Run code on samples | Your run code raises an error on a sample case                    |
| Metric return value | `score` does not return a valid score and feedback                |

> [!NOTE]
> You can start the run only after the contract check passes.

## 6. Start and watch

The summary screen shows "Ready to run". Its tabs are General, Data, Models, Budget and Reward. Check the setup, then start the optimization.

Open the run from your optimizations list to watch it. Read [Results](/docs/guides/results) to understand the scores and the winning version.

## Next steps

- Write a better metric: [Metrics and scorers](/docs/concepts/scorers).
- Learn which engines fit your artifact: [How optimization works](/docs/concepts/optimization).
- Learn what a run costs: [Billing](/docs/concepts/billing).
