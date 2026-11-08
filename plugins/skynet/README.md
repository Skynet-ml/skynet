# Skynet plugin

Skynet improves your code against a metric that you write. This plugin
connects Claude and Codex to your Skynet account. Your assistant can set up
a run on a GitHub repository step by step, check its price, start it with a
spending limit that you approve, follow the run, show the result, list your
runs and stop a run.

You need a Skynet account at [skynetml.com](https://skynetml.com). A run
spends at most the limit that you approve, from your existing balance.
Unused budget goes back to your balance when the run ends.

## Contents

| Path | What it is |
| --- | --- |
| `.mcp.json` | The hosted Skynet MCP server, `https://mcp.skynetml.com/mcp` (Streamable HTTP, OAuth 2.1). |
| `skills/optimize/` | Sets up a repository run, checks its price, gets your agreement to a limit, starts the run and presents the result. |
| `skills/manage-runs/` | Lists runs, shows a run's status, spend and result, and stops a run after you confirm it. |
| `.claude-plugin/plugin.json` | Claude Code and Claude plugin manifest. |
| `.codex-plugin/plugin.json` | Codex and ChatGPT plugin manifest. |
| `assets/` | The Skynet logo, light and dark, 512 x 512 PNG. |

## Tools

| Tool | Access | What it does |
| --- | --- | --- |
| `repo_draft_start` | Write | Starts a repository run draft with the objective, and lists the engines. |
| `repo_draft_set_repository`, `repo_draft_set_data`, `repo_draft_set_scorer`, `repo_draft_set_models`, `repo_draft_set_engine`, `repo_draft_set_budget` | Write | Each saves one part of the draft: the repository and editable paths, the cases, the scorer, the models, the engine and the search length. Each reports what to fix and the next tool. |
| `repo_draft_review` | Read | Checks the whole draft. Suggests a spending limit and shows the spendable balance. Spends nothing. |
| `repo_draft_submit` | Write | Starts the run under a new budget of `max_cost_cents`, after you agree to it. |
| `get_run` | Read | Returns a run's status, budget and, when it is finished, its result. |
| `list_runs` | Read | Lists your runs, newest first. |
| `stop_run` | Write | Stops a run. Spend stops and the unused budget is released. |

`check_run` and `start_run` stay listed only to refuse old clients. They
start no run.

The tools cannot add funds, change balances or delete runs.

## Install

### Claude Code

```bash
claude plugin marketplace add Skynet-ml/skynet --sparse .claude-plugin plugins
claude plugin install skynet@skynet
```

Then run `/mcp` in a session, select `skynet` and sign in. The skills are
`/skynet:optimize` and `/skynet:manage-runs`. Claude also uses them without
the slash command when your request fits.

### Codex

```bash
codex plugin marketplace add Skynet-ml/skynet --sparse .agents/plugins --sparse plugins
```

Then open `/plugins` in Codex, install Skynet, and sign in when Codex asks.

### Claude (web and desktop) and ChatGPT

Install Skynet from the Claude directory or the ChatGPT plugin directory
when the listing is live. Then connect the Skynet connector and sign in.

## Sign in

The first tool call opens the Skynet sign-in page. Approve the requested
access:

- `runs:read`: review run drafts, read runs and results.
- `runs:write`: build run drafts, start and stop runs.

## Support

- Report problems at
  [github.com/Skynet-ml/skynet/issues](https://github.com/Skynet-ml/skynet/issues).
- [Privacy policy](https://skynetml.com/privacy) and
  [terms of service](https://skynetml.com/terms).

## License

Apache-2.0. See [LICENSE](LICENSE). The rest of the Skynet repository is
AGPL-3.0.
