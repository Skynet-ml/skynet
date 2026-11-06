# Skynet MCP server

Skynet has one MCP server. An outside agent (Claude Code, Codex, Cursor or
any standard MCP client) uses it to work with the whole platform. The agent
can also set up a repository run step by step and start it.

The hosted app and the Mac app serve the same server. Only the sign-in is
different.

## Connect to the hosted server

| Environment | URL |
| --- | --- |
| Production | `https://mcp.skynetml.com/mcp` |
| Staging | `https://mcp.staging.skynetml.com/mcp` |
| PR preview | `https://api-pr-<n>.staging.skynetml.com/remote-mcp` |

The server uses OAuth. Add the URL to your client. The client opens a browser
page. Sign in and approve the scopes:

- `runs:read` lets the agent read runs and call read routes.
- `runs:write` lets the agent change data and start runs.

Example for Claude Code:

```bash
claude mcp add --transport http skynet https://mcp.skynetml.com/mcp
```

## Connect to the Mac app

The Mac app runs the backend on this machine. At start, the backend writes
`mcp.json` into its state folder (`APP_LOCAL_STATE_DIR`). The file has mode
`0600` and holds two values:

- `url`: the local server, for example `http://127.0.0.1:8000/remote-mcp`.
- `token`: a local bearer token. It stays the same across restarts.

Give the client the URL and send the token in the `Authorization` header:

```bash
claude mcp add --transport http skynet-local http://127.0.0.1:8000/remote-mcp --header "Authorization: Bearer <token from mcp.json>"
```

The local token acts for the Mac app's linked account and has both scopes. A
hosted server refuses it. Do not copy the token into logs or shared files.

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
| Models | `repo_draft_set_models` (reflection model, token source, more models) | Yes |
| Engine | `repo_draft_set_engine` (one engine id from the start answer) | Yes |
| Budget | `repo_draft_set_budget` (scorer runs, iterations, stop score) | No |
| Review | `repo_draft_review` | Yes |
| Submit | `repo_draft_submit` | Yes |

`repo_draft_review` gives the minimum spending limit, a suggested limit and
the amount the account can spend now (`spendable_cents`). The agent must show
the price to the user.
`repo_draft_submit` starts the run only with `confirm=true`, after the user
agrees to `max_cost_cents`. A limit below the minimum is refused.

A retry with the same arguments does not start a second run. The default
idempotency key comes from the draft and its contents.

A draft expires 7 days after its last change, and the tools refuse it. Each
`repo_draft_start` removes expired drafts and the oldest drafts above 20.

The hosted server runs `managed` and `byok` models. The Mac app runs
`subscription` and `byok` models. The models step reports any other token
source as an issue. In the Mac app, name a subscription model
`subscription/<codex|claude>/<model>`, so Skynet knows which CLI runs it.

The scorer is `score(repo_path, case)`. It returns a `(score, feedback)` pair
or a `{"score": ..., "feedback": ...}` dict. The engines learn from the
feedback, so a bare number fails the run. A bare number on only some paths,
such as an error path, gives a note: the runner refuses only those scores. A scorer that takes only `repo_path`
also works, but the step adds a note when the draft has data: every case gets
the same score.

After the submit, use `get_run`, `list_runs` and `stop_run` to follow the run.

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
