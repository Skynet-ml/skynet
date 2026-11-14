# Skynet MCP server

Skynet has one MCP server. An outside agent (Claude Code, Codex, Cursor or
any standard MCP client) uses it to work with the whole platform. The agent
can also set up a repository run step by step and start it.

The hosted app and the Mac app serve the same server and the same OAuth
connect flow. Only the URL is different.

## Connect to the hosted server

| Environment | URL |
| --- | --- |
| Production | `https://mcp.skynetml.com/mcp` |
| Staging | `https://mcp.staging.skynetml.com/mcp` |

The server uses OAuth. Add the URL to your client. The client opens a browser
page. Sign in and approve the scopes:

- `runs:read` lets the agent read runs and call read routes.
- `runs:write` lets the agent change data and start runs.

Example for Claude Code:

```bash
claude mcp add --transport http skynet https://mcp.skynetml.com/mcp
```

## Connect to the Mac app

The Mac app serves the same OAuth connect flow on this machine. Its API
uses the first free port from 48100 to 48119, so the URL is usually
`http://127.0.0.1:48100/remote-mcp`. The app log shows the port in the
`ports api=...` line.

Add the URL to your client:

```bash
claude mcp add --transport http skynet-local http://127.0.0.1:48100/remote-mcp
```

The client opens the Mac app's consent page in your browser. The Mac app
signs in to its own account by itself. Approve the scopes. The agent then
acts for the Mac app's account.

If the app starts on another port, connect the client again with the new
URL.

## Set up and start a repository run

The `repo_draft_*` tools follow the same steps as the setup screen in the
app. Each step saves into a draft. Each answer gives feedback, so the agent
knows what to fix and what to do next:

- `saved`: the part the step stored. A step stores its part also when it
  has issues.
- `issues`: problems to fix before the run can start.
- `notes`: facts the agent can show the user.
- `remaining_steps`: required steps that are not done.
- `next_tool`: the tool to call next.

| Step | Tool | Required |
| --- | --- | --- |
| Start | `repo_draft_start` (objective, name). It lists the engines. | Yes |
| Repository | `repo_draft_set_repository` (owner/name, branch or commit, editable paths, setup command) | Yes |
| Data | `repo_draft_set_data` (inline cases or a staged dataset) | No |
| Scorer | `repo_draft_set_scorer` (Python metric code) | Yes |
| Contract | `repo_draft_set_contract` (the `optimize.toml` text) | When the repository has no `optimize.toml` |
| Models | `repo_draft_set_models` (reflection model, token source, more models) | Yes |
| Engine | `repo_draft_set_engine` (one engine id from the start answer) | Yes |
| Budget | `repo_draft_set_budget` (scorer runs, iterations; the run stops at `[outcome] target` in optimize.toml) | No |
| Review | `repo_draft_review` | Yes |
| Setup check | `repo_draft_preflight` (paid; it runs the setup check of the run once) | No |
| Submit | `repo_draft_submit` (optional `hardening_job_id`) | Yes |

Every new run needs an `optimize.toml` contract, in the draft or in the
repository. `repo_draft_contract_guide` gives the format guide and an example
file. The setup screen's agent uses the same guide. `repo_draft_set_contract`
saves the text into the draft, and the run writes it over the repository's
own file. A parser error is an issue with the parser's message. When the
repository has an `optimize.toml`, the step is optional. When the draft and
the repository both have none, `repo_draft_review` reports an issue and the
run does not start.

`repo_draft_review` gives the minimum spending limit, a suggested limit and
the amount the account can spend now (`spendable_cents`). The agent must show
the price to the user.
`repo_draft_submit` starts the run only with `confirm=true`, after the user
agrees to `max_cost_cents`. A limit below the minimum is refused.

A retry with the same arguments does not start a second run. The default
idempotency key comes from the draft, its contents, `max_cost_cents` and
`hardening_job_id`.

`repo_draft_preflight` also needs `confirm=true` and `max_cost_cents`. It
runs the setup check of `/wizard/preflight` under the budget that the
submit uses. A submit with the same arguments reuses the check and does not
pay for it again.

`hardening_job_id` attaches a metric-hardening job of the account to the
run. The run's PR ships the job's exploit suite. Start the job with the
`platform_*` hardening tools.

A draft expires 7 days after its last change, and the tools refuse it. Each
`repo_draft_start` removes expired drafts and the oldest drafts above 20.

The hosted server runs `managed` and `byok` models. The Mac app runs
`subscription` and `byok` models. The models step reports any other token
source as an issue. In the Mac app, name a subscription model
`subscription/<codex|claude>/<model>`, so Skynet knows which CLI runs it.

The scorer is `score(repo_path, case)`. It returns a `(score, feedback)` pair
or a `{"score": ..., "feedback": ...}` dict. The engines learn from the
feedback. A bare number on any path, an error path too, stops the whole run. A scorer that takes only `repo_path`
also works, but the step adds a note when the draft has data: every case gets
the same score.

After the submit, use `get_run`, `get_run_progress`, `list_runs` and
`stop_run` to follow the run. `get_run_progress` returns the progress events
after `since`, `next_since` for the next call, the best score and the
candidate count.

## Score your own versions with the judge

`repo_judge_score` scores a version that the agent wrote itself. It uses
the draft's repository, metric and contract, and Skynet runs no engine.
Finish the draft and call `repo_draft_review` first.

- Send `draft_id`, `max_cost_cents` and `confirm=true`, after the user
  agrees to `max_cost_cents`.
- Send exactly one candidate: `candidate_commit`, the full 40-character
  SHA of a pushed commit that descends from the base commit, or
  `candidate_patch`, a `git diff` against the base commit.
- `max_cost_cents` is the limit of the whole session. It must be at least
  two times the draft's minimum: one scored call and the final score. A
  lower limit gets `budget.below_minimum`, which names this minimum.

The first call opens a judge session. It creates one budget and one run,
and the answer gives the `session_id`. Skynet pins the base commit on that
call. Later calls with the same `draft_id` and `max_cost_cents` score in the
same session. While a session is open, the draft steps refuse a change
(`judge.session_open`), and a call with another limit is refused
(`judge.limit_changed`).

Each call reads `optimize.toml` at the base commit. Skynet refuses a
candidate that changes `optimize.toml` or a protected path
(`judge.candidate_refused`), and the refusal names every broken rule. Skynet
also refuses a `candidate_commit` that it cannot fetch or that does not
descend from the base commit. It refuses a `candidate_commit` that changes a
text file that is not UTF-8, and a candidate that is too long. These
refusals also answer `judge.candidate_refused`, and Skynet does not store
them. When a fetch fails because of a network error or a timeout, call again
with the same arguments. The candidate runs in its own sandbox, and the
metric runs in a separate sandbox. The answer gives the mean `score`, the
`named_scores`, the `feedback` and the result of each search case. The
answer never holds the metric code or a held-out case. A retry with the same
candidate returns the stored score and does not pay again, also when the
budget cannot pay for a new call. An answer with `status: in_progress`
means: call again with the same arguments. Skynet answers `in_progress`
while other calls of the session hold its budget, and after a temporary
repository or sandbox failure while the candidate is scored. A retry after
such a failure pays for its own work. After three temporary failures of the
same candidate, the call answers `judge.score_failed`. Any other failure,
for example a saved secret that the user deleted, answers
`judge.score_failed` at once, and a retry returns the same error.

Only the budget or `stop_run` with the `session_id` ends a session. A bulk
cancel skips a judge session. Before each call, the budget must still pay
for one call (the draft's minimum) and a reserve for the final score. The
reserve is one minimum at first. After scored calls, it is 1.5 times the
mean spend per search case times the number of final cases, and never less
than one minimum. When the budget cannot pay for that, the call ends the
session. While usage of an earlier call still settles, it answers
`in_progress` instead. The end scores the best version once on the held-out cases. With
fewer than five cases, nothing is held out, and the end scores the best
version on every case. With no cases, it scores the version once.

The end waits (`budget.pending`) while a call of the session still runs,
also when the budget is closed.
After a temporary repository or sandbox failure of the final score, the end
also answers `budget.pending`, and the session stays open. The next
`stop_run` or call scores again. After three such failures, the session
ends `failed`. Any other failure of the final score, for example a saved
secret that the user deleted, ends the session `failed` at once. A call
that starts after the end starts scores nothing. It
gets `in_progress` while the end runs. After that, it drives an end that
waits or that stopped before it finished, and then gets the ended answer.
Every later end keeps the `ended_by` of the first end. When a call stops at the budget limit, the end still tries the
final score with the money that is left. When that money cannot pay for
it, the session ends `stopped`, and its message says that no held-out score
exists. One call can still spend more than the reserve, because Skynet does
not cap the spend of a single call.

`get_run` gives the result: `details.judge` holds the call count, the
`best_patch_sha256`, the search score and the held-out score, and
`details.record` holds the run record with the final `score`. Skynet opens
no pull request for the best version.

## Platform tools

Each REST route that carries the `agent` OpenAPI tag is also a tool, with
the `platform_` prefix. These are the routes the in-app agent uses. The tool
calls the route as the connected account:

- `GET`, `HEAD` and `OPTIONS` routes need `runs:read`.
- Other methods need `runs:write`. `DELETE` tools are marked destructive.
- Accounts on the admin allowlist cannot use platform tools.

Some agent routes are not tools:

- Routes that start paid work without the cost review: `/optimize`, clone,
  retry, resume, restart and `/serve/*`. Use the `repo_draft_*` flow.
- Billing routes that change data (`POST`, `PUT`, `PATCH` and `DELETE` on
  `/billing/*`). The MCP server never adds funds or changes billing. Billing
  read routes, such as the wallet, are tools.
- Routes that only drive the in-app UI, such as opening a page or a dialog.
