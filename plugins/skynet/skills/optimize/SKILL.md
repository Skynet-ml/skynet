---
name: optimize
description: Optimize the code in a GitHub repository against a metric with Skynet. Use when the user asks to improve, tune or optimize code against test cases or a score, or asks to run a Skynet optimization. Builds a repository run draft step by step with the repo_draft tools, reviews its price, gets the user's agreement to a spending limit, submits the run, follows it with get_run and presents the result.
---

# Optimize with Skynet

Skynet improves the code in a GitHub repository against a metric that the
user owns. It needs the Skynet MCP server of this plugin. The first tool call
asks the user to sign in to their Skynet account.

The tools:

| Tool | What it does | Cost |
| --- | --- | --- |
| `repo_draft_start` | Starts a run draft with the objective. Returns the `draft_id` and the list of engines. | Free |
| `repo_draft_set_repository` | Sets the repository, its branch or commit, and the paths the run may change. | Free |
| `repo_draft_set_data` | Sets the cases the scorer runs on. Optional. | Free |
| `repo_draft_set_scorer` | Sets the Python scorer that grades each version. | Free |
| `repo_draft_set_models` | Sets the model of the coding agent and who pays for its calls. | Free |
| `repo_draft_set_engine` | Sets the optimization engine. | Free |
| `repo_draft_set_budget` | Sets how many scorer runs the search can use. Optional. | Free |
| `repo_draft_review` | Checks the whole draft and returns the smallest and the suggested `max_cost_cents`, models without a price, and the spendable balance. | Free |
| `repo_draft_submit` | Starts the run under a new budget of `max_cost_cents`. | Spends up to `max_cost_cents` from the user's balance |
| `get_run` | Returns the status, the budget and, for a finished run, the result. | Free |
| `list_runs` | Lists the user's runs, newest first. Use it to find a run after an unclear submit result. | Free |

`check_run` and `start_run` refuse every call. Do not use them.

Every draft step answers with `issues`, `notes`, `remaining_steps` and
`next_tool`. Fix every issue before you go on. Tell the user the notes that
change their plan. Then call `next_tool`.

## Workflow

1. **Collect the inputs.** You need:
   - the objective in one or two sentences;
   - the GitHub repository (`owner/name`), and the branch or commit if it is
     not the default branch;
   - the files and folders the run may change (`.` is the whole repository);
   - the metric: how to score one version of the repository;
   - test cases, if the metric needs them: pairs of `input` and an optional
     `expected`.
   Ask the user for what is missing. Do not invent test cases or expected
   outputs without the user's agreement. A private repository needs GitHub
   connected in the Skynet app. You cannot connect it.
2. **Start the draft.** Call `repo_draft_start` with the objective and a
   short `name`. Keep the `draft_id`.
3. **Set the repository.** Call `repo_draft_set_repository` with the
   repository, `editable_paths`, and `branch` or `commit`. Leave
   `setup_command` empty unless the user gives one. Skynet then infers it.
4. **Set the data** (optional). Call `repo_draft_set_data` with `cases`.
   The candidate code gets only `input`. The scorer gets the whole case.
   Skip this step when the scorer needs no cases.
5. **Set the scorer.** Write Python that defines one of:
   - `score(repo_path)`: the run has no cases;
   - `score(repo_path, case)`: the scorer runs once for each case;
   - `score(repo_path, case, output=None)`: `output` is what the
     repository's `optimize.toml` `[run]` entry returned for the case, and
     `None` when nothing ran.
   Return `(score, "<short feedback>")` or
   `{"score": <number>, "feedback": "<short text>"}`. Higher is better.
   Skynet refuses a bare number, because the engines learn from the
   feedback. Call `repo_draft_set_scorer` with the code as `metric_code`.
6. **Set the models.** Call `repo_draft_set_models` with the
   `reflection_model` that the user names. It is the model of the coding
   agent that writes each version. Keep `token_source` at `managed` unless
   the user asks for their own key (`byok`). Do not offer `subscription`:
   this server refuses it, because a subscription model runs only in the
   Mac app. Set
   `scorer_model` only when the scorer calls an LLM.
7. **Set the engine.** Offer the available engines from the
   `repo_draft_start` answer: only those with `available: true`. A
   repository run uses exactly one engine, and `auto` is not available for
   it. Use the engine that the user names. If the user has no preference,
   show the available engines and ask the user to pick one. Call
   `repo_draft_set_engine`.
8. **Set the budget** (optional). Call `repo_draft_set_budget` only when the
   user wants a different search length. The default is 200 scorer runs.
   This is not a money limit.
9. **Review the draft.** Call `repo_draft_review`. If `ready` is false, fix
   each issue with the step tool that the issue is about, then review again.
   Do not rely on `next_tool` here: when every step is set, it is
   `repo_draft_review`. The price fields are missing until every step is
   done. They can come with blocking issues, so use them only when `ready`
   is true. If
   `unpriced_models` is not empty, tell the user that those models have no
   price and ask which model to use instead.
10. **Get the user's agreement to a limit.** Show
    `suggested_max_cost_cents`, `min_max_cost_cents` and `spendable_cents` in
    dollars. Say that the run stops when it reaches the limit, and that
    unused budget goes back to the balance when the run ends. **Never call
    `repo_draft_submit` before the user agrees to a specific amount.** If the
    balance is lower than the minimum, tell the user to add funds at
    skynetml.com. You cannot add funds.
11. **Submit the run.** Call `repo_draft_submit` with the `draft_id`, the
    agreed `max_cost_cents`, `confirm: true` and a new `idempotency_key`
    (for example a UUID). Keep the key.
    - If the result has `status: in_progress`, wait `retry_after_seconds`.
      Then call `repo_draft_submit` again with the same arguments and the
      same key. Do not ask the user to agree again. If the result is still
      `in_progress` 15 minutes after the first call, stop and show the
      `detail` to the user.
    - If a call times out or the result is unclear, call `list_runs` first.
      If no run exists, call `repo_draft_submit` again with the same
      arguments and the same key, never a new key.
    - If Skynet returns an error with a `code`, do not retry unchanged.
      `draft.not_ready` comes with `issues`: fix them. `budget.below_minimum`
      comes with `detail` and `min_max_cost_cents`. Show the `code` and the
      reason, fix the draft, review it again, get the user's agreement to a
      limit again, and submit with a new `idempotency_key`.
    - `budget.conflict` means that the key already has a higher limit. A
      reused key keeps the first limit. If the error has an
      `optimization_id`, follow that run with `get_run`. Otherwise submit
      with a new key.
    - Keep the `optimization_id` from the result.
12. **Follow the run** with `get_run`. Poll about once a minute. Stop when
    `terminal` is true, or when `run.status` is `paused`. A paused run waits
    for the user to resume it on the run page. Tell the user and stop
    polling. Tell the user the status and the spend from `execution_budget`.
    The run page is `https://skynetml.com/optimizations/<optimization_id>`.
13. **Present the result.** From `result`, show:
    - `baseline_score` and `best_score`, and `metric_improvement`;
    - `best_feedback`, when present;
    - `best_candidate`: the winning version, as a change to the starting
      commit. The run page shows its diff and downloads the repository with
      it applied.
    For a `failed`, `stopped` or `cancelled` run, show `run.message` and
    `run.stop_reason`. Offer to apply the change to the user's files. Do not
    apply it without the user's agreement.

## Example

A ticket classifier in `acme/support-bot`, in `classifier/`:

1. `repo_draft_start`: objective "Classify a support ticket as billing, bug
   or other.", name "Ticket classifier".
2. `repo_draft_set_repository`: repository `acme/support-bot`,
   `editable_paths` `["classifier/"]`.
3. `repo_draft_set_data`: cases
   `[{"input": {"ticket": "I was charged twice."}, "expected": "billing"},
   {"input": {"ticket": "The export button does nothing."}, "expected": "bug"}]`.
4. `repo_draft_set_scorer`: this `metric_code`:

   ```python
   def score(repo_path, case, output=None):
       ok = output == case["expected"]
       return (1.0 if ok else 0.0), "correct" if ok else f"expected {case['expected']}, got {output}"
   ```

   The repository's `optimize.toml` `[run]` entry runs the classifier on
   each case's input.
5. `repo_draft_set_models`, `repo_draft_set_engine`, `repo_draft_review`,
   the user's agreement, then `repo_draft_submit`.

## Errors

- A tool error with `insufficient_scope` means the connection lacks a
  scope. Ask the user to reconnect the Skynet server and approve it.
- A tool error with a `code` and `detail` comes from Skynet. Show the
  detail to the user. Do not retry a refused run with a higher limit
  without the user's agreement.
- A draft expires after 7 days without changes. Each account keeps at most
  20 drafts, and `repo_draft_start` deletes the oldest. If a step says the
  `draft_id` is unknown or expired, start a new draft.
- `status: in_progress` from `repo_draft_submit` is a wait, not a refusal
  or an error. Follow step 11 of "Workflow": retry with the same key, for at
  most 15 minutes.
