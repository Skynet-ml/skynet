Skynet sets limits on runs, files, storage and request rates. The limits keep the platform stable and protect your balance. This page lists the default values. An administrator can change some limits for an account.

> [!NOTE]
> The values on this page are the current defaults. When you reach a limit, the app or the API tells you which limit stopped the request.

## Runs

| Limit                        | Free | Pro   |
| ---------------------------- | ---- | ----- |
| Active runs at the same time | 5    | 20    |
| Saved runs                   | 100  | 1,000 |

An active run is a run with the status `pending`, `validating`, `running` or `paused`. When you have the maximum number of active runs, Skynet refuses new runs until one of them finishes.

The saved-run limit counts all your runs. Delete runs that you do not need to make room for new ones.

If a Pro renewal fails, the account keeps the Pro limits for 7 days. After that, it uses the Free limits until the payment succeeds.

| Limit                                 | Value                                                       |
| ------------------------------------- | ----------------------------------------------------------- |
| Copies in one API clone request       | 1 to 5                                                      |
| Automatic recovery attempts for a run | 3                                                           |
| Sample cases in the contract check    | 3                                                           |
| Stored progress events for each run   | 5,000. Skynet removes the oldest events first.              |
| Lifetime of one sandbox               | 45 minutes. A run that needs more time opens a new sandbox. |

Read [Runs](/docs/concepts/runs) for how recovery attempts work.

## Storage

| Limit         | Free    | Pro    |
| ------------- | ------- | ------ |
| Total storage | 500 MiB | 20 GiB |

Total storage includes your runs, datasets, logs, agent chats, staged uploads and search data. Skynet refuses a write that would go over your storage limit.

## Files and datasets

| Limit                                      | Value                                   |
| ------------------------------------------ | --------------------------------------- |
| Dataset file saved to your library         | 50 MiB for each file, after compression |
| Rows from a connector that imports records | 20,000                                  |
| File attached to an agent chat             | 20 MiB                                  |
| Text read from one chat attachment         | 50,000 characters                       |
| Audio clip for dictation                   | 25 MB                                   |
| GitHub repository archive                  | 200 MiB, compressed                     |
| Repository file opened on the run page     | 1 MiB                                   |

Connectors that import records include database tables, LLM traces and Notion databases. Read [Prepare a dataset](/docs/guides/datasets).

### Workspace files in the wizard

| Limit                        | Value                                                                   |
| ---------------------------- | ----------------------------------------------------------------------- |
| Extra files in the workspace | 20                                                                      |
| Length of a file name        | 120 characters                                                          |
| Folder depth                 | 3 folders                                                               |
| File types                   | `.py`, `.txt`, `.md`, `.json`, `.yaml`, `.csv`, `.toml`, `.ini`, `.cfg` |

`run.py` and `metric.py` are the entry files of a run. You cannot delete, rename or move them.

## Request rates

| Limit                                                           | Value                                       |
| --------------------------------------------------------------- | ------------------------------------------- |
| Run submissions                                                 | 30 for each account in a rolling minute     |
| Dictation clips                                                 | 120 for each account in an hour             |
| Files read for agent chats                                      | 300 for each account in an hour             |
| Account requests (register, password reset, email verification) | 20 for each email address in a rolling hour |
| Wizard assistant turns                                          | 50 for each account in a UTC day            |
| Search and indexing tokens                                      | 2,000,000 for each account in a month       |

When you go over a rate limit, the API returns HTTP status `429`. Wait, then send the request again.

When an account goes over its monthly search and indexing tokens, Skynet stops indexing new items for that account. Search then works as if semantic search is not available.

## Money

| Limit                                             | Value                                                                   |
| ------------------------------------------------- | ----------------------------------------------------------------------- |
| Default spending limit for a new run              | $5, or the amount needed to start if that is more                       |
| Spare room in the amount needed to start          | $1, or the cost of the most expensive single model call if that is more |
| Custom top-up                                     | $5 to $1,000                                                            |
| Balance needed to start an interactive model turn | $0.05                                                                   |

Read [Budgets and billing](/docs/concepts/billing) for fees and reservations.

## Related pages

- [Runs](/docs/concepts/runs)
- [Budgets and billing](/docs/concepts/billing)
- [FAQ](/docs/faq)
