Skynet improves an artifact that you already have. You give it a starting version, a set of cases and a metric. Skynet proposes new versions, runs each one on your cases, scores the output with your metric, and keeps the best version.

## What Skynet optimizes

Skynet works on two artifact kinds:

| Artifact kind | What changes                                              |
| ------------- | --------------------------------------------------------- |
| Repository | The files in the paths that you allow |
| DSPy program  | The instructions of the predictors in a `dspy.Module`     |

Every run uses the same contract. Your run code defines `run(artifact, input)`. Your metric defines `score(input, output, expected=None)`. The metric returns a score and written feedback. The optimizer reads the feedback to make the next version better.

> [!NOTE]
> Your run code and your metric run in a sandbox, in separate processes. The run code cannot see the metric source or the expected values.

## One product, many engines

Skynet is Optimize Anything. The artifact kind decides which engines can work on it:

- Repositories use the native engines: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler and ShinkaEvolve.
- DSPy program artifacts use the DSPy optimizers: DSPy GEPA, DSPy MIPROv2 and DSPy BootstrapFewShot.

On a DSPy program, **Auto** uses DSPy GEPA. On a repository, you select one native engine. Read [How optimization works](/docs/concepts/optimization) and [Engines](/docs/concepts/engines).

## How a run works

1. You describe the task and set the starting version.
2. You give the cases. Skynet splits them into train, validation and test sets.
3. You select an optimizer, the models and a budget.
4. You write the run code and the metric. A contract check runs them on sample cases.
5. Skynet runs the optimization and shows the results.

Skynet selects versions on the validation cases. It reports the final score on the test cases. The engines never see the test cases.

## Where to start

| If you want to                    | Read                                                  |
| --------------------------------- | ----------------------------------------------------- |
| Start your first run              | [Quickstart](/docs/quickstart)                        |
| Understand how optimization works | [How optimization works](/docs/concepts/optimization) |
| Choose an engine                  | [Engines](/docs/concepts/engines)                     |
| Prepare your data                 | [Datasets and splits](/docs/concepts/datasets)        |
| Write a metric                    | [Metrics and scorers](/docs/concepts/scorers)         |
| Fill in the wizard                | [Submit a run](/docs/guides/submit)                   |
| Read your results                 | [Results](/docs/guides/results)                       |
| Understand cost                   | [Billing](/docs/concepts/billing)                     |
| Use Skynet from an agent          | [MCP](/docs/mcp)                                      |
| Call Skynet from code             | [API](/docs/api)                                      |
| Check the limits                  | [Limits](/docs/limits)                                |

> [!TIP]
> Start with a small dataset and the default settings. Make the metric feedback specific. Good feedback gives the optimizer more to work with than the score alone.
