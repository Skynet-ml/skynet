# Skynet plugin

Skynet improves a DSPy program against a metric that you write. This plugin
connects Claude and Codex to your Skynet account. Your assistant can check a run spec before it runs, start a run with a
spending limit that you approve, follow the run, show the result, list your
runs and stop a run.

You need a Skynet account at [skynetml.com](https://skynetml.com). A run
spends at most the limit that you approve, from your existing balance.
Unused budget goes back to your balance when the run ends.

## Contents

| Path | What it is |
| --- | --- |
| `.mcp.json` | The hosted Skynet MCP server, `https://mcp.skynetml.com/mcp` (Streamable HTTP, OAuth 2.1). |
| `skills/optimize/` | Builds a run spec, checks it, gets your agreement to a limit, starts the run and presents the result. |
| `skills/manage-runs/` | Lists runs, shows a run's status, spend and result, and stops a run after you confirm it. |
| `.claude-plugin/plugin.json` | Claude Code and Claude plugin manifest. |
| `.codex-plugin/plugin.json` | Codex and ChatGPT plugin manifest. |
| `assets/` | The Skynet logo, light and dark, 512 x 512 PNG. |

## Tools

| Tool | Access | What it does |
| --- | --- | --- |
| `check_run` | Read | Checks a run spec without running it. Suggests a spending limit and shows the spendable balance. |
| `start_run` | Write | Starts a run under a new budget of `max_cost_cents`. |
| `get_run` | Read | Returns a run's status, budget and, when it is finished, its result. |
| `list_runs` | Read | Lists your runs, newest first. |
| `stop_run` | Write | Stops a run. Spend stops and the unused budget is released. |

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

- `runs:read`: check run specs, read runs and results.
- `runs:write`: start and stop runs.

## Support

- Report problems at
  [github.com/Skynet-ml/skynet/issues](https://github.com/Skynet-ml/skynet/issues).
- [Privacy policy](https://skynetml.com/privacy) and
  [terms of service](https://skynetml.com/terms).

## License

AGPL-3.0-only, the same as the Skynet repository.
