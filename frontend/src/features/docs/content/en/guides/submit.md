The submit wizard sets up and starts a run. It has four steps: **Task**, **Inputs**, **Optimizer** and **Metric & run**. Each step has one or more screens. This guide explains each screen, from the task to the check and the start of the run.

> [!NOTE]
> The wizard shows more or fewer settings for each experience level: **Guided**, **Standard** or **Expert**. Guided is the default. Change the level in **Settings → Account**, in **How much to show**. Some labels are simpler at the Guided level. This guide uses the Standard and Expert labels and gives the Guided label in brackets where it is different.

## Step 1: Task

### What do you want to optimize?

Describe your task to the assistant in the **Task interview** chat. The assistant writes two fields for you:

- **Objective** (Guided: **Goal**): what the optimized code must do well.
- **Background** (Guided: **Extra context**): context, constraints and what a good result looks like.

You can edit both fields. The assistant does not overwrite your edits.

### Where the code comes from

Skynet is Optimize Anything: every run optimizes a repository. Select one of two choices:

- **Use a repository**: optimize code in a GitHub repository that you connected.
- **Create a new repository**: Skynet creates a new GitHub repository in your account. The assistant writes a first solution from your task and data. That solution is the starting version.

Connect GitHub in **Settings → Connectors** before you start.

### Use a repository

Set these fields:

- **Repository**: pick a repository, or write it as `owner/name`.
- **Commit**: a 40-character SHA. Leave it empty to use the latest commit on the branch.
- **Paths the optimizer may change**: one file or folder on each line. A dot means the whole repository.
- **Setup command**: runs in each fresh checkout before scoring. Leave it empty and Skynet finds the command.

### Create a new repository

Set these fields:

- **Name**: the name of the new repository. Your GitHub account must not already have a repository with this name.
- **Private** or **Public**: who can see the repository on GitHub.

Skynet pushes each candidate to its own branch. The `skynet/best` branch points at the best version.

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

- **Reflection model**: the model that reads the scores and feedback and writes the next candidates. With a coding-agent optimizer, this is the agent's model.
- **Task model**: the model that your repository code and your scorer can call through Skynet's model gateway. Read [How optimization works](/docs/concepts/optimization).

### Budget

Set how long the run may keep trying, and how much it may spend:

- **Max scorer runs** (Guided: **Max tries**): the most candidates that Skynet scores. Some optimizers need a minimum number.
- **Stop at score** (Guided: **Stop early once the score reaches**): the run stops when a version reaches this score.
- **Max iterations** (Guided: **Max rounds**): shown for optimizers that support it.
- **Spending limit**: the most the run may spend. The default is $5, or the amount **Needed to start** if that is more. You can also select **No limit**.

The wizard does not let you continue if the limit or your balance is below the amount needed to start. Read [Budgets and billing](/docs/concepts/billing).

## Step 4: Metric & run

### Scorer

Write `scorer.py`. It defines `score(repo_path, case)` and returns a score with feedback. The assistant can write it for you. Read [Metrics and scorers](/docs/concepts/scorers).

The repository can have a `serve.py` file with `def serve(input)`. Skynet uses it to serve the best version after the run. Read [Read the results](/docs/guides/results).

### Secrets

Add secrets, such as API keys that your code needs. Each secret reaches your code and your scorer as an environment variable with the same name. The coding agent cannot see them. Skynet hides their values in the logs.

### Check

The check (Guided: **Test run**) runs the setup and your scorer on the starting version in the sandbox. It checks:

- the scorer signature,
- the setup command,
- the scorer return value on the sample cases.

Select **Check** (Guided: **Test it**). You cannot start the run until the check passes. If you change the scorer or the setup after a pass, run the check again.

> [!TIP]
> Open each sample to see its score and feedback. Good feedback gives the optimizer more to work with than the score alone.

### Scheduled run

You can run the optimization again on a schedule after it succeeds. Each scheduled cycle starts from the current best version of the repository. The default is **Off**.

### Ready to run

The last screen shows a summary in five tabs: **General**, **Data**, **Models**, **Budget** and **Reward**. Check the setup, then start the optimization.

The summary also shows the run's visibility. New runs are public by default. Change it later in the **Share and privacy** dialog on the run page. Read [Share and publish](/docs/guides/sharing).

## After you start

The run page opens. Read [Runs](/docs/concepts/runs) and [Read the results](/docs/guides/results).
