Skynet improves code that you already have, or code that it writes for you. Skynet is Optimize Anything: every run optimizes a repository. You give it a repository, a set of cases and a scorer. Skynet proposes new versions of the repository, scores each one with your scorer, and keeps the best version.

## What Skynet optimizes

Every run optimizes a repository. You can start in two ways:

| Start                   | What happens                                                                                   |
| ----------------------- | ---------------------------------------------------------------------------------------------- |
| Use a repository        | Skynet changes the files in the paths that you allow, in a GitHub repository that you connect. |
| Create a new repository | You give a task and data. Skynet creates a new GitHub repository in your account and writes a first solution. |

The code in the repository can be anything: a prompt pipeline, an agent harness, a script or a library. It can use any library, for example DSPy.

Every run uses the same contract. Your scorer defines `score(repo_path, case)`. It returns a score and written feedback. The optimizer reads the feedback to make the next version better.

> [!NOTE]
> Each version runs in a sandbox. The coding agent that writes versions cannot see the scorer source or the expected values.

## One product, many engines

**Auto** is the default for every run. It tries GEPA, AutoResearch, Meta-Harness, AutoSaddler and ShinkaEvolve, then continues GEPA from the best version. You can also select one engine: GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler or ShinkaEvolve. Read [How optimization works](/docs/concepts/optimization) and [Engines](/docs/concepts/engines).

## How a run works

1. You describe the task, and pick a repository or ask Skynet to create one.
2. You give the cases. Skynet splits them into train, validation and test sets.
3. You select an optimizer, the models and a budget.
4. You write the scorer. A setup check runs it on the starting version.
5. Skynet runs the optimization and shows the results.

Skynet selects versions on the validation cases. It reports the final score on the test cases. The engines never see the test cases.

## Where to start

| If you want to                    | Read                                                  |
| --------------------------------- | ----------------------------------------------------- |
| Start your first run              | [Quickstart](/docs/quickstart)                        |
| Understand how optimization works | [How optimization works](/docs/concepts/optimization) |
| Choose an engine                  | [Engines](/docs/concepts/engines)                     |
| Prepare your data                 | [Datasets and splits](/docs/concepts/datasets)        |
| Write a scorer                    | [Metrics and scorers](/docs/concepts/scorers)         |
| Fill in the wizard                | [Submit a run](/docs/guides/submit)                   |
| Read your results                 | [Results](/docs/guides/results)                       |
| Understand cost                   | [Billing](/docs/concepts/billing)                     |
| Use Skynet from an agent          | [MCP](/docs/mcp)                                      |
| Call Skynet from code             | [API](/docs/api)                                      |
| Check the limits                  | [Limits](/docs/limits)                                |

> [!TIP]
> Start with a small dataset and the default settings. Make the scorer feedback specific. Good feedback gives the optimizer more to work with than the score alone.
