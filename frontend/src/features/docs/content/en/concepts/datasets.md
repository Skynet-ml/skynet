A dataset is a table of cases. Each row is one case. Skynet scores each version of your repository on the cases with your scorer, and uses the scores to compare versions. This page explains how Skynet reads the cases, what the column roles do, and how it splits the cases into train, validation and test sets.

## Load the data

In the **Inputs** step of the wizard, you have two options:

- **Upload a CSV, JSON or Excel file**. Skynet accepts `.csv`, `.json`, `.xlsx` and `.xls` files.
- **Pick from library**. Use a dataset that you saved before.

A run can have at most 200,000 cases.

Read [Datasets](/docs/guides/datasets) to manage saved datasets.

## Column roles

Under **How to read the data**, give each column one role:

| Role   | What Skynet does with the column                         |
| ------ | -------------------------------------------------------- |
| Input  | Puts the value in `case["input"]`, which your scorer receives  |
| Output | Puts the value in `case["expected"]`, which your scorer receives |
| Skip   | Does not use the column                                  |

Each column also has a kind: **Text** or **Image**.

Only one column can be the Output column. If you set a second column to Output, the first one changes to Skip.

### What your code receives

For each row, Skynet builds one case:

- `input` is a dict. It has one key for each Input column.
- `expected` is a dict with one key: the name of the Output column.

For this table:

| message             | label   | id  |
| ------------------- | ------- | --- |
| I was charged twice | billing | 17  |

with the roles `message` = Input, `label` = Output and `id` = Skip, your code receives:

```python tab="input"
{"message": "I was charged twice"}
```

```python tab="expected"
{"label": "billing"}
```

> [!TIP]
> Read the expected value by the column name in your scorer, for example `case["expected"]["label"]`.

## Data modes

The roles set the data mode of the run:

| Mode   | When                 | What happens                                                                                 |
| ------ | -------------------- | -------------------------------------------------------------------------------------------- |
| Pairs  | One column is Output | Every row is a case with an input and an expected value. You need at least 2 rows.           |
| Single | No column is Output  | The run uses the Input columns of the first row only. It is one case with no expected value. |

In single mode, Skynet uses the one case for training, selection and the report. Nothing is held out.

## The split

In pairs mode, Skynet splits the cases into three sets:

| Set              | What it is for                                         |
| ---------------- | ------------------------------------------------------ |
| Train            | The engine proposes and tests versions on these cases. |
| Val (validation) | Skynet selects the best version on these cases.        |
| Test             | Skynet reports the final score on these cases.         |

> [!NOTE]
> The test cases never reach an engine, a proposer or the contract check. The final score is on cases that the optimizer did not see.

### The default split

| Number of pairs | Train | Val | Test |
| --------------- | ----- | --- | ---- |
| 5 or more       | 70%   | 15% | 15%  |
| Fewer than 5    | 85%   | 15% | 0%   |

With fewer than 5 pairs, Skynet keeps no test set.

### How Skynet counts the cases

Skynet turns the fractions into whole numbers of cases with the largest-remainder method. The counts always add up to the number of cases. A set with a fraction above zero gets at least one case. A set with a fraction of zero gets no cases.

Before the split, Skynet shuffles the cases. The shuffle uses the seed of the run. If you give no seed, Skynet makes one from the cases, the objective and the artifact. So if you submit the same run again, you get the same split.

### Fallbacks

- With fewer than 5 pairs, Skynet removes the test set and scales train and val so that they add up to 1.
- If train is empty, Skynet uses the val cases as train.
- If there is no val set, Skynet selects on the train cases.
- If there is no test set, Skynet reports on val. If there is no val set either, it reports on train.

> [!WARNING]
> A score that Skynet reports on train or val is not a held-out score. Use at least 5 pairs to get a test set.

## Change the split

The split section shows **Recommended split**. It has two modes:

1. **Use recommendation**: Skynet uses the default split above.
2. **Manual selection**: you set **Train**, **Val** and **Test** yourself.

In manual selection, each field takes a fraction from 0 to 1, in steps of 0.05. Each field shows the number of cases it gets. The three fractions must add up to 1. If they do not, the wizard shows "The split must add up to 1."

## Contract samples

The contract check in the **Metric & run** step runs your setup and your scorer on the starting version, on 3 sample cases. Skynet takes these samples from the train and val cases only, never from test.

## Related pages

- [Datasets](/docs/guides/datasets)
- [Metrics and scorers](/docs/concepts/scorers)
- [Submit a run](/docs/guides/submit)
- [Limits](/docs/limits)
