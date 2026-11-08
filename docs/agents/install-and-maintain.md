# Install and maintain (agent-ready)

Reference for coding agents. The always-on rules are in `AGENTS.md`.

One command installs a fresh clone or worktree. A second command keeps the
codebase healthy. Each one is a script that writes a log, plus an agent
prompt that reads the log.

| Recipe | What it does |
| --- | --- |
| `just setup` | Deterministic install: `uv sync`, `npm ci`, env files (never overwritten), Postgres/LiteLLM checks, migrations, i18n check. Safe to run again. |
| `just install-agent` | Runs the Setup hook (`claude --init-only`), then `/install`: the agent checks the log, fixes known issues, runs tests and reports. |
| `just install-hil` | Same as `install-agent`, but the agent asks the human before each choice. |
| `just maintain` | Report only: npm audit, pip-audit, Dependabot alerts, outdated deps, Alembic heads, merged worktrees, Docker, untracked files. |
| `just maintain-agent` | Runs `just maintain`, then `/maintain`: the agent explains the findings and proposes fixes as a PR. |

- Scripts: `scripts/agent/init.sh`, `scripts/agent/maintain.sh`.
- Logs: `.claude/logs/init-latest.log`, `.claude/logs/maintain-latest.log`
  (gitignored). Logs never hold secret values.
- Prompts: `.claude/commands/install.md`, `install-hil.md`, `maintain.md`.
  Each has a **Common issues** list. Add a Problem / Solution pair when you
  solve a new failure.
- Claude Code: `.claude/settings.json` wires the scripts to the `Setup` hook
  (`init` matcher: `claude --init-only` or `claude -p --init`;
  `maintenance` matcher: `claude -p --maintenance`).
- Codex, Factory, Pi and other harnesses: run `just setup` or
  `just maintain`, then follow the matching prompt file.

## CI checks with Dagger

GitHub Actions runs the CI gates as Dagger checks. You can run the same
checks on your machine. You need Docker and the Dagger CLI v1.0.0-beta.25
(`curl -fsSL https://dl.dagger.io/dagger/install.sh | DAGGER_VERSION=v1.0.0-beta.25 sh`).

```bash
dagger generate -y                     # only after a module change
dagger check -l                        # list the checks
dagger check skynet-ci/frontend-lint   # run one check
dagger check 'skynet-ci/*'             # run every check
```

- The module is `.dagger/modules/skynet-ci/`. `dagger.toml` and `dagger.lock`
  are at the repo root. Commit `dagger.lock`.
- The generated `sdk/`, `clients/` and `uv.lock` in the module are committed.
  CI does not generate them. After a module change, run `dagger generate -y`
  and commit the result.
- The checks use cache volumes for uv, npm and pip. The second run is fast.
- A check fails with exit code 1 and prints the failed command's output.
- Do not log in to Dagger Cloud. The checks need no secrets.
