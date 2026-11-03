The submit wizard sets up and starts a run. It has four steps: **Task**, **Inputs**, **Optimizer** and **Metric & run**. Each step has one or more screens. This guide explains each screen, from the task to the contract check and the start of the run.

> [!NOTE]
> The wizard shows more or fewer settings for each experience level: **Guided**, **Standard** or **Expert**. Guided is the default. Change the level in **Settings → Account**, in **How much to show**. Some labels are simpler at the Guided level. This guide uses the Standard and Expert labels and gives the Guided label in brackets where it is different.

## Step 1: Task

### What do you want to optimize?

Describe your task to the assistant in the **Task interview** chat. The assistant writes two fields for you:

- **Objective** (Guided: **Goal**): what the optimized artifact must do well.
- **Background** (Guided: **Extra context**): context, constraints and what a good result looks like.

You can edit both fields. The assistant does not overwrite your edits.

### What will be optimized

Select the **Artifact type**:

| Artifact type | What changes                                     |
| ------------- | ------------------------------------------------ |
| Prompt        | Instructions sent to a language model            |
| Code          | The source of a program                          |
| DSPy program  | The predictor instructions of a DSPy module      |
| Text          | Any other text, such as a document or a template |

Then give the **Starting version** (Guided: **Where we start from**). Paste the current version, or let the assistant write a draft.

### Optimize a repository

You can also optimize code in a repository. Set these fields:

- **Repository**: pick a repository, or write it as `owner/name`.
- **Commit**: a 40-character SHA. Leave it empty to use the latest commit on the branch.
- **Paths the optimizer may change**: one file or folder on each line. A dot means the whole repository.
- **Setup command**: runs in each fresh checkout before scoring. Leave it empty and Skynet finds the command.

## Step 2: Inputs

### Data

Add the cases for the run. You can:

- Upload a CSV, JSON or Excel file.
- Pick a dataset from your library.

Read [Datasets and splits](/docs/concepts/datasets) and [Prepare a dataset](/docs/guides/datasets).

### How to read the data

The assistant reads the data and suggests how to use it. Check its choices:

- Mark each column as an input, the expected output, or ignore it.
- With no output column, Skynet uses the first row as one input.
- Set the split into train, validation and test sets. The split must add up to 1.

> [!WARNING]
> You cannot open the later steps while the **Inputs** step has a problem. Fix the issues that the wizard shows, then continue.

## Step 3: Optimizer

At the Standard level, this step has one screen. It shows the optimizer, the budget and, when needed, the models. At the Guided and Expert levels, the step has three screens: **Optimizer**, **Models** and **Budget**.

### Optimizer

The wizard lists only the optimizers that fit your task and inputs. **Auto** is selected for you. Select another optimizer if you want to. Read [Engines](/docs/concepts/engines) and [How optimization works](/docs/concepts/optimization).

### Models

- **Task model**: the model that your run code calls. Leave it empty if your run code does not call a model. A DSPy program must have a task model.
- **Reflection model**: the model that reads the scores and feedback and writes the next candidates. With a coding-agent optimizer, this is the agent's model.

### Budget

Set how long the run may keep trying, and how much it may spend:

- **Max scorer runs** (Guided: **Max tries**): the most candidates that Skynet scores. Some optimizers need a minimum number.
- **Stop at score** (Guided: **Stop early once the score reaches**): the run stops when a version reaches this score.
- **Max iterations** (Guided: **Max rounds**): shown for optimizers that support it.
- **Spending limit**: the most the run may spend. The default is $5, or the amount **Needed to start** if that is more. You can also select **No limit**.

The wizard does not let you continue if the limit or your balance is below the amount needed to start. Read [Budgets and billing](/docs/concepts/billing).

## Step 4: Metric & run

### Code and metric

Write two files:

- `run.py`: the run code that runs your artifact on one case.
- `metric.py`: the metric that scores the output and writes feedback.

The **Coding agent** can write and change any file for you. It checks its code and fixes what fails. You can add up to 20 extra files. Read [Metrics and scorers](/docs/concepts/scorers).

The metric sets its model in its own `llm()` call. Without `model=`, the metric uses the reflection model.

### Contract check

The **Contract check** (Guided: **Test run**) runs your code and metric in the sandbox on 3 sample cases. It checks:

- the run code signature,
- the metric signature,
- the metric model,
- the run code on the samples,
- the metric return value.

Select **Check** (Guided: **Test it**). You cannot start the run until the check passes. If you change the code or the setup after a pass, run the check again.

> [!TIP]
> Open each sample to see its input, output and feedback. Good feedback gives the optimizer more to work with than the score alone.

### Scheduled run

You can run the optimization again on a schedule after it succeeds. The default is **Off**.

### Ready to run

The last screen shows a summary in five tabs: **General**, **Data**, **Models**, **Budget** and **Reward**. Check the setup, then start the optimization.

The summary also shows the run's visibility. New runs are public by default. Change it later in the **Share and privacy** dialog on the run page. Read [Share and publish](/docs/guides/sharing).

> [!NOTE]
> A repository run has only the **Code and metric** screen and the summary in this step. Skynet checks the setup against the repository when you start the run.

## After you start

The run page opens. Read [Runs](/docs/concepts/runs) and [Read the results](/docs/guides/results).
