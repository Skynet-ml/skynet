# Skynet Justfile — task runner for dev, test, and agent workflows
# Usage: just <recipe> [args]
# Requires: https://github.com/casey/just

# Backend dependency manager: "uv" (default) or "pip". Toggle per-invocation
# with `just pkg=pip test`, or persist it with `export SKYNET_PKG=pip`.
pkg := env_var_or_default("SKYNET_PKG", "uv")
# Command prefix that runs inside the backend env. uv wraps with `uv run`
# (auto-syncing on demand); pip runs the tool directly, so `just install` the
# deps first. `_dev` additionally pulls the dev extra under uv.
_run := if pkg == "uv" { "uv run " } else { "" }
_dev := if pkg == "uv" { "uv run --extra dev " } else { "" }

default:
    @just --list

# Install backend deps (with dev tools) for the selected manager. The pip path
# needs this once before backend/test/lint; the uv path resolves on demand.
install:
    cd backend && {{ if pkg == "uv" { "uv sync --extra dev" } else { 'pip install -e ".[dev]"' } }}

# Steps: deps, env files, Postgres/LiteLLM checks, migrations, i18n check.
# Log: .claude/logs/init-latest.log
# Deterministic install for a fresh clone or worktree (safe to run again)
setup:
    bash scripts/agent/init.sh

# `claude --init-only` runs the Setup hook (init); /install does the rest.
# Other harnesses: run `just setup`, then follow .claude/commands/install.md.
# Install, then an agent checks the log, fixes known issues and reports
install-agent:
    claude --init-only
    claude "/install"

# Install with an agent that asks you before each choice
install-hil:
    claude --init-only
    claude "/install-hil"

# Covers advisories, Dependabot, outdated deps, Alembic heads, worktrees,
# Docker and leftover files. Log: .claude/logs/maintain-latest.log
# Health report for the codebase (report only)
maintain:
    bash scripts/agent/maintain.sh

# Other harnesses: run `just maintain`, then follow .claude/commands/maintain.md.
# Health report, then an agent explains it and proposes fixes as a PR
maintain-agent:
    -bash scripts/agent/maintain.sh
    claude "/maintain"

backend:
    cd backend && {{_run}}python main.py

frontend:
    cd frontend && npm run dev

# Start both (backend in background, frontend in foreground)
dev:
    cd backend && {{_run}}python main.py &
    cd frontend && npm run dev

test-unit:
    cd backend && {{_dev}}pytest core/ tests/unit/ -v

# Requires running server + OPENAI_API_KEY
test-integration:
    cd backend && {{_dev}}pytest tests/test_llm_integration.py -v

# Start a local AWS Emulator in Docker (docs/agents/aws-emulator.md)
aws-emulator:
    bash scripts/aws-emulator.sh start

# Stop the local AWS Emulator
aws-emulator-stop:
    bash scripts/aws-emulator.sh stop

# Opt-in S3, EC2 and deploy-script tests on the emulator; a skip counts as a failure
test-aws-emulator: aws-emulator
    cd backend && AWS_EMULATOR_REQUIRED=1 {{_dev}}pytest tests/aws_emulator -v
    AWS_EMULATOR_REQUIRED=1 bash infra/terraform/scripts/tests/test-aws-emulator.sh

# Frontend type check via build
test-frontend:
    cd frontend && npm run test:unit && npm run build

test: test-unit test-frontend

lint-backend:
    cd backend && {{_run}}ruff check .

lint-frontend:
    cd frontend && npm run lint

lint: lint-backend lint-frontend

format:
    cd backend && {{_run}}ruff format .

fix:
    cd backend && {{_run}}ruff check --fix .

# Verify the generated i18n artefacts are in sync with i18n/locales/he.json.
# Delegates to the script's built-in --check mode (renders artefacts in
# memory and exits non-zero on drift, without touching the working tree).
check-i18n:
    python3 scripts/generate_i18n.py --check

info:
    @echo "Project: Skynet"
    @echo "Branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'not a git repo')"
    @echo "Python: $(python3 --version 2>/dev/null || echo 'not installed')"
    @echo "Node: $(node --version 2>/dev/null || echo 'not installed')"

cli:
    claude

plan task:
    claude -p "/scout-and-plan {{task}}"

implement task:
    claude -p "/implement {{task}}"

review:
    claude -p "/review"
