A dataset is a table of examples that an optimization run learns from and scores against. This guide shows how to upload a file, edit rows and columns, set column roles, and share a dataset. For the concepts, read [Datasets](/docs/concepts/datasets).

## Supported files

Skynet reads these file types:

| Extension       | How Skynet reads it                                                                  |
| --------------- | ------------------------------------------------------------------------------------ |
| `.csv`          | The first line is the header row. The file needs at least one data row.              |
| `.json`         | An array of objects, or one object. The keys of the first object become the columns. |
| `.xlsx`, `.xls` | Skynet reads only the first sheet.                                                   |

Skynet rejects other extensions with the message "Unsupported file format".

> [!TIP]
> Put the expected output in the last column. The submit wizard marks the last column as the output by default.

### Example CSV

```csv
question,answer
What is the capital of France?,Paris
How many legs does a spider have?,8
```

### Example JSON

```json
[
  { "question": "What is the capital of France?", "answer": "Paris" },
  { "question": "How many legs does a spider have?", "answer": "8" }
]
```

## Upload a dataset

1. Open **Datasets**.
2. Click **Upload dataset**, or drag a file onto the page. The page shows "Drop to upload" while you drag.
3. Wait for the toast "Dataset saved to your library".

Skynet uses the file name without its extension as the dataset name. To change it, select the dataset and click **Rename**.

> [!NOTE]
> Each account has a storage quota. If an upload goes over the quota, Skynet opens a dialog that explains the limit. See [Limits](/docs/limits).

You can also upload a file in the submit wizard ("Upload a CSV, JSON or Excel file") or attach one in the [agent panel](/docs/guides/agent-panel).

## Find a dataset

The library lists your datasets. Use **Search by name** to filter the list. Datasets that other people share with you appear under **Shared with me**.

When you select one dataset, these actions appear:

- **Tag this dataset** opens the [tagger](/docs/guides/tagger) with this dataset.
- **Edit data** opens the dataset editor.
- **Share** opens the share dialog.
- **Rename** changes the dataset name.

Other actions are **Save a copy** and **Delete**.

> [!WARNING]
> Delete removes the dataset from your library permanently. You cannot undo it. Runs built from the dataset stay valid, but their link back to it breaks.

## Edit a dataset

The editor shows the dataset as a grid. It shows 100 rows on each page. Use **Next rows** and **Previous rows** to move between pages.

### Change rows and columns

- **Add row** and **Add column** add new cells at the end.
- **Insert row above** and **Insert row below** add a row next to the selected row.
- **Duplicate row**, **Delete row**, **Delete column** and **Rename column** change the structure.
- **Drag to fill** copies a cell value into the cells next to it.
- **Undo** and **Redo** reverse or repeat your last change.

Double-click a row to view it in full.

### Saving

The editor saves your changes automatically, shortly after each edit. The status shows **Saving…** while it saves. If the save fails, the status shows **Saving failed**.

> [!NOTE]
> The editor cannot save a dataset with no rows. It shows "Add a row to save your changes" until you add one.

Click **Back to the library** when you finish.

### Read-only access

If you have the Viewer role on a shared dataset, the editor opens in read-only mode. To change the data, ask the owner for the Editor role, or use **Save a copy**.

### Tag with AI

The editor has a **Tag with AI** action. It opens the tagger, where an AI model helps you add labels. See [Label data with the tagger](/docs/guides/tagger).

## Set column roles

A run must know which columns are inputs and which column is the expected output. You set these roles when you submit a run, not in the dataset editor.

In the submit wizard, the step "How to read the data" shows each column with three choices:

- **Input**: your scorer receives this value in `case["input"]`.
- **Output**: the expected answer that the scorer compares against.
- **Skip**: the run ignores this column.

The defaults are:

- Every column is an input, and the last column is the output.
- A dataset with one column has one input and no output.

With no output column, Skynet uses the first row as one input. You must pick at least one input column.

Some columns can also hold images. For those columns, choose **Image** or **Text** to tell Skynet how to read the values.

> [!TIP]
> The dataset editor shows an "output" badge on output columns. A new column that you add in the editor starts as an input.

The wizard also splits the rows into parts for training and evaluation. The parts must add up to 1. The wizard shows "The split must add up to 1." when they do not. For the full flow, read [Submit a run](/docs/guides/submit).

## Share a dataset

Select a dataset and click **Share**. The **Share and privacy** dialog opens.

### Link access

| Option                   | Who can open the dataset                                      |
| ------------------------ | ------------------------------------------------------------- |
| **Restricted**           | Only the owner and invited people.                            |
| **Anyone with the link** | Any signed-in person with the link, with the role you choose. |

For an "Anyone with the link" dataset, choose the role that the link gives: **Viewer** or **Editor**. Click **Copy link** to copy the URL.

### Invite people

1. Under **Invite people**, type a username in **Add people by username**.
2. Click **Invite**.
3. Check the list under **People with access**. Use **Change role** to change the role of a person.

The roles are:

| Role   | Access                                                    |
| ------ | --------------------------------------------------------- |
| Owner  | Full control, including managing sharing and deletion.    |
| Editor | Viewer access plus running the model, chat, and renaming. |
| Viewer | Opens the dataset. The editor is read-only.               |

### Transfer ownership

Use **Transfer ownership** to give the dataset to another person. Skynet asks you to confirm.

> [!WARNING]
> After the transfer, you become an editor and lose owner privileges.

## Next steps

- [Submit a run](/docs/guides/submit) with your dataset.
- [Label data with the tagger](/docs/guides/tagger) if your rows have no labels yet.
- Read about [scorers](/docs/concepts/scorers) to choose how Skynet compares outputs.
