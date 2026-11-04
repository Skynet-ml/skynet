---
name: optimize
description: Optimize a DSPy program against a metric with Skynet. Use when the user asks to improve, tune or optimize a DSPy program or its instructions against test cases or a score, or asks to run a Skynet optimization. Builds the run spec, checks it with check_run, gets the user's agreement to a spending limit, starts the run with start_run, follows it with get_run and presents the result.
---

# Optimize with Skynet

Skynet improves a DSPy program against a metric that the user owns. It needs
the Skynet MCP server of this plugin. The first tool call asks the user to sign in to their Skynet account.

The tools:

| Tool | What it does | Cost |
| --- | --- | --- |
| `check_run` | Checks a run spec without running it. Returns the static checks, the smallest and the suggested `max_cost_cents`, models without a price, and the spendable balance. | Free |
| `start_run` | Starts a run under a new budget of `max_cost_cents`. | Spends up to `max_cost_cents` from the user's balance |
| `get_run` | Returns the status, the budget and, for a finished run, the result. | Free |
| `list_runs` | Lists the user's runs, newest first. Use it to find a run after an unclear `start_run` result. | Free |

## Workflow

1. **Collect the inputs.** You need:
   - the objective in one or two sentences;
   - the starting version of the artifact;
   - test cases: one case (`single` mode) or two or more input and expected
     pairs (`pairs` mode);
   - the metric: how to score one output.
   Ask the user for what is missing. Do not invent test cases or expected
   outputs without the user's agreement.
2. **Write the workspace code.** Every run uses one contract:
   - `run.py` builds a `dspy.Module` at module level and defines
     `def run(artifact, input)`, which calls the module. If the file has more
     than one module, name the one to optimize `program`. `input` is the
     case's input dict. Return the output.
   - `metric.py` defines `def score(input, output, expected=None)`. Return
     `(score, {"feedback": "<short text>"})`: a number (higher is better)
     and a dict whose `feedback` string explains the score. Skynet refuses a
     bare number or a plain string, because the optimizers learn from the
     feedback.
   - In either file, `from skynet import llm` gives a model client:
     `llm(prompt, input)` sends `prompt` as the system message and `input`
     as the user message, and returns text. In `run.py` it uses the task
     model. In `metric.py` it is available only when the spec sets a
     `metric_model`.
3. **Build the spec** (see the template below). Use the model ids that the
   user names. Keep `optimizer.choice` at `auto` unless the user asks for a
   specific optimizer.
4. **Call `check_run`** with the spec. Fix every failed check and call it
   again. If `unpriced_models` is not empty, tell the user that those models
   have no price and ask which model to use instead.
5. **Get the user's agreement to a limit.** Show `suggested_max_cost_cents`,
   `min_max_cost_cents` and `spendable_cents` in dollars. Say that the run
   stops when it reaches the limit, and that unused budget goes back to the
   balance when the run ends. **Never call `start_run` before the user
   agrees to a specific amount.** If the balance is lower than the minimum,
   tell the user to add funds at skynetml.com. You cannot add funds.
6. **Call `start_run`** with the same spec, the agreed `max_cost_cents` and
   a new `idempotency_key` (for example a UUID). If the result has
   `status: in_progress`, wait `retry_after_seconds`, then call `start_run`
   again with the same arguments and the same key. Do not ask the user to
   agree again. The setup check can take several minutes. If the result
   is still `in_progress` 15 minutes after the first call, stop and show
   the `detail` to the user. Keep the key. Before any later `start_run`
   for this spec, call `list_runs` and use the run if it exists. If no
   run exists, call `start_run` with the same arguments and the same key,
   never a new key: a run still in its setup check does not show in
   `list_runs`. If a
   call times out or the result is unclear, call `list_runs` first. If no
   run exists, call `start_run` again with the same arguments and the same
   `idempotency_key`. If Skynet
   returns a spec or limit error with a `code`, do not retry unchanged.
   Show the `code` and `detail`, fix the spec, call `check_run` again, get
   the user's agreement to a limit again, and call `start_run` with a new
   `idempotency_key`. A reused key keeps the first limit. Keep the
   `optimization_id`.
7. **Follow the run** with `get_run`. Poll about once a minute. Stop when
   `terminal` is true, or when `run.status` is `paused`. A paused run waits
   for the user to resume it on the run page. Tell the user and stop
   polling. Tell the user the status and the spend from
   `execution_budget`. The run page is
   `https://skynetml.com/optimizations/<optimization_id>`.
8. **Present the result.** From `result`, show:
   - `baseline_score` and `best_score`, and `metric_improvement`;
   - `best_candidate`, in full, as the improved version;
   - `split_counts.held_out`: if it is false, say that the scores come from
     cases the optimizer also used;
   - `best_feedback`, when present.
   For a `failed`, `stopped` or `cancelled` run, show `run.message` and
   `run.stop_reason`. Offer to apply `best_candidate` to the user's files.
   Do not apply it without the user's agreement.

## Spec template

```json
{
  "name": "Ticket classifier",
  "task": {
    "objective": "Classify a support ticket as billing, bug or other.",
    "artifact": {
      "kind": "dspy_program",
      "starting_version": "Classify the ticket. Answer with one word: billing, bug or other."
    }
  },
  "data": {
    "mode": "pairs",
    "cases": [
      {"input": {"ticket": "I was charged twice."}, "expected": "billing"},
      {"input": {"ticket": "The export button does nothing."}, "expected": "bug"}
    ]
  },
  "optimizer": {"choice": "auto"},
  "models": {
    "task_model": {"name": "<model id>"},
    "reflection_model": {"name": "<model id>"}
  },
  "workspace": {
    "run_code": "import dspy\n\nprogram = dspy.Predict(\"ticket -> label\")\n\n\ndef run(artifact, input):\n    return program(ticket=input[\"ticket\"]).label.strip().lower()\n",
    "metric_code": "def score(input, output, expected=None):\n    ok = output == expected\n    return (1.0 if ok else 0.0), {\"feedback\": \"correct\" if ok else f\"expected {expected}, got {output}\"}\n"
  }
}
```

Notes on the spec:

- `task.artifact.kind` is `dspy_program`. Repository runs start from the
  Skynet app, not from this plugin.
- `models.reflection_model` is required. `task_model` is the model that
  the DSPy program in `run.py` uses. Add `metric_model` only when the metric calls
  `llm`.
- `optimizer.budget.max_scorer_runs` (default 200) caps how many times the
  metric runs. It is not a money limit. The money limit is
  `max_cost_cents` on `start_run`.
- Do not set `workspace.files`. DSPy optimizers load `run.py` alone.
- Do not set `max_cost_cents`, `execution_budget_id` or `preflight_id` in
  the spec. `start_run` sets the budget.

## Errors

- A tool error with `insufficient_scope` means the connection lacks a
  scope. Ask the user to reconnect the Skynet server and approve it.
- A tool error with a `code` and `detail` comes from Skynet. Show the
  detail to the user. Do not retry a refused run with a higher limit
  without the user's agreement.
- `status: in_progress` from `start_run` is a wait, not a refusal or
  an error. Follow step 6 of "Workflow": retry with the same key,
  for at most 15 minutes.
