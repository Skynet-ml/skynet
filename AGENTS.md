# AGENTS.md — Skynet working rules

Skynet optimizes DSPy programs and repositories. Stack: FastAPI (`backend/`), Next.js (`frontend/`), PostgreSQL.

Every coding agent (Devin, Claude Code, Codex and others) reads this file
first. Its rules always apply; nested `AGENTS.md` files only add rules. Read
the matching file below when the task needs it:

| When you | Read |
| --- | --- |
| Open, update, describe, review, record videos for, or merge a PR | `.agents/skills/skynet-pr-workflow/SKILL.md` |
| Sign in to staging or a PR preview, or run acceptance checks there | `.agents/skills/skynet-test-accounts/SKILL.md` |
| Work on or debug a PR preview environment | `docs/agents/pr-previews.md` |
| Need the stack, layout, local URLs, persistence, auth or i18n facts | `docs/agents/architecture.md` |
| Install a worktree or run maintenance | `docs/agents/install-and-maintain.md` |
| Move code between files or add a router or feature slice | `docs/agents/refactoring.md` |

Put personal, machine-specific rules in `CLAUDE.local.md` (Claude Code) or
`AGENTS.local.md` (Devin). Both are gitignored; never commit them.

## Verification (MANDATORY before every push)

```bash
# Backend: tests for the touched areas, and lint.
cd backend && .venv/bin/python -m pytest <touched areas> -q   # or: uv run --extra dev pytest <paths> -q
ruff check <touched files>                                     # full gate: just lint-backend
cd backend && uv run --extra dev pytest core/ tests/unit/ -v   # credential-free core/unit suite

# Frontend CI gates, and ESLint on touched files.
cd frontend && npm run typecheck && npm run lint && npm run test:unit
cd frontend && npm run build                                   # production build

# i18n: catalog drift.
just check-i18n
```

- New UI strings go into the source catalogs: `i18n/locales/ui/he.json` (base)
  **and** `en.json`. Then run `python3 scripts/generate_i18n.py`.
- `backend/tests/test_llm_integration.py` needs a running server and paid
  model credentials. It is not part of the credential-free PR gate.
- Billing changes must also pass `core/billing/tests/test_budgets_postgres.py`
  against a real PostgreSQL URL in `SKYNET_BUDGET_TEST_DB_URL`.
- Backend dependency or runtime changes must keep `uv.lock` and the pip
  requirements files aligned, and pass the stable and preview Docker builds.
- Lint, typecheck and build are not behavior tests. Say which is which.
- Local frontend work: do not drive a browser unless the task asks for it.
  Tell the user what to check at `localhost:3000`. PR acceptance checks and
  videos still follow the `skynet-pr-workflow` skill.

Local dev servers: backend `:8000` (`cd backend && .venv/bin/python main.py`
or `just backend`), frontend `:3000` (`cd frontend && npm run dev` or `just
frontend`), or both with `just dev`. The backend needs Postgres on `:5432` and,
for LLM features, a LiteLLM proxy on `:4000` (`deploy/litellm/`,
`backend/.env.example`). Install: `docs/agents/install-and-maintain.md`.

## Core working rules (MANDATORY)

- **Think first.** State your assumptions. If a request has more than one
  meaning, name the meanings; do not pick one silently. If something is
  unclear, stop and ask. If a simpler approach exists, say so.
- **Simplicity.** Write the minimum code that solves the problem. No
  features, abstractions, configuration or error handling that nobody asked
  for. If 200 lines can be 50, rewrite them.
- **Surgical changes.** Touch only what the task needs. Match the existing
  style. Do not refactor or reformat code that is not broken. Remove only the
  imports, variables and functions that your change made unused. Mention
  unrelated dead code; do not delete it.
- **Goal-driven.** Turn the task into checks you can verify ("write a test
  that reproduces the bug, then make it pass"). For multi-step work, state a
  short plan with a check for each step. Loop until the checks pass.
- **Start fast.** Do not ask questions when the intent is clear. Do not read
  unrelated files first.
- **Generated code.** When code generates other code, edit the source, never
  the output. Check that generated HTML/JS is valid.
- **Hebrew and RTL.** Find-and-replace can corrupt Hebrew text. Check the
  direction, alignment and position of RTL UI.

## Git, authorship and public-repo safety (MANDATORY)

- **No AI attribution.** No `Co-Authored-By: <AI>` lines. Never name Claude,
  Codex, Devin, OpenAI, Anthropic or any AI assistant as an author or
  contributor in commits, tags, PR titles, PR bodies, issue comments,
  release notes or changelogs. The author is the human who runs the session.
- **Never push to `main` or `staging` directly.** Create one branch and one
  worktree per issue from current `origin/staging`. Use a conventional
  commit message. Push the branch and open a **draft** PR against `staging`.
- The PR may touch only the files in its Linear issue's `## Files` list
  (`.github/workflows/pr-files-scope.yml` checks this).
- Never bypass branch protection or hooks. Never force-push.
- The repository is **public**. Never commit secrets, passwords, tokens,
  emails, account data or videos. Secret names are fine.

## Merge policy (MANDATORY)

- Each PR gets a preview (staging copy, empty database) at
  `https://pr-<n>.staging.skynetml.com`. Run acceptance checks and videos there.
- **Fix bugs locally, deploy once.** Each push rebuilds the preview and
  starts a new review. Test and review the full diff locally, then push once.
- **Only the orchestrator merges**, in the daily window (03:00
  America/Los_Angeles) or when Gilad asks. Implementation and review agents
  never merge, and never post `Approved for merge` or `Changes requested:`.
- Backend-only PRs (`backend/**`, optional `docs/**/*.md`) merge when all
  gates pass. UI PRs (`frontend/`) and other PRs wait for the user's approval.
  A change in `staging` is approved for production. The orchestrator promotes
  `staging` → `main` when the staging checks pass. A manual hold stops it.
  Steps, gates, proof card and Linear flow: the `skynet-pr-workflow` skill.

## Test accounts and environments (MANDATORY)

- Run acceptance checks on AWS staging (`https://staging.skynetml.com`, API
  `https://api-staging.skynetml.com`) or the PR's preview. Never test against
  production. Railway is deleted; never use it.
- Use only the test account that setup gave you. If it is missing, report
  the exact setup failure and record the coverage gap. Do not ask Gilad to
  create one. Never sign up for a normal check. Never borrow a personal
  session. Never print or commit passwords, tokens, cookies or email codes.
- Never add funds, grant credits, change balances or change live billing
  configuration. Never deploy production.
- Sign-in, sign-up exceptions, served-commit check: `skynet-test-accounts`.

## Writing rule (MANDATORY)

Write every message, report, PR description, Linear comment and commit message
in ASD-STE100 Simplified Technical English. The reader works with many agents
and must understand the state quickly. These rules do not change code,
identifiers or quoted text:

- Short sentences: 20 words or fewer for instructions, 25 for descriptions.
  One idea in each sentence.
- Simple, common words. One word for one meaning, every time.
- Active voice and simple tenses. No "has been" or "had done".
- Instructions are direct commands ("Run the tests.").
- Start each update with where the work is now, then what comes next.

## Feature gate: challenge every feature (MANDATORY)

Skynet has **no customer requests** yet. Before you build a new feature,
screen, setting, option or integration, it must clearly serve one bucket:

- **Business**: more money or a better business model (activation,
  conversion, retention, paid usage, lower cost to serve). Name the number.
- **Surprise and delight**: users would show it to someone. Name the surprise.

If it fits neither, push back: "Which number does it move, or what's the
surprise?" Ask: what if we never build it ("nothing much" means don't)? Is
it the current bottleneck? Does it improve the core product, or only cover a
weak spot (tours, nags, settings that patch bad defaults)? If it passes,
build the smallest version: delete requirements with no clear owner and
reason; do not abstract, configure or automate before the manual version
works. Push hard once, then respect the user's decision. Small fixes, bug
fixes and refactors the user asked for skip the gate. Full pass: `/scope`.

## Commenting, docstring & import style (MANDATORY, backend Python)

Applies to every `backend/` file you touch (not `.venv/`, `__pycache__/` or
`alembic/versions/`); bring it into compliance. Ruff does not check these.

- **Docstrings.** A module docstring in every file. A Google-style docstring
  on every function and method, public and private: one-line imperative
  summary, `Args:`, `Returns:`, and `Raises:` only for a non-obvious failure.
  Skip `Args:`/`Returns:` only when both are trivial and the summary covers them.
- **Imports only at the top of the file. No exceptions.** Optional deps use a
  module-level `try/except ImportError` that sets the name to `None`. Tests
  that need a fresh import use `importlib.import_module`. Fix circular
  imports structurally (slim `__init__.py`, leaf modules, `TYPE_CHECKING`),
  never with inline imports.
- **Comments say why, never what.** Keep only hidden constraints,
  workarounds, surprising behavior, subtle invariants, design decisions and
  ticket references. Delete comments that restate code, label sections or
  echo identifiers.
- **Pydantic class docstrings are part of the OpenAPI contract.** Pydantic
  emits a `BaseModel` class docstring into `openapi.json` as
  `components.schemas.<Name>.description`. Never add or remove one,
  including when you move a class into another router. No test guards
  this, so compare the schema before and after by hand. To document a
  class for readers, put a comment above it. Details:
  `docs/agents/refactoring.md`.

## Product and runtime model

- **Skynet is Optimize Anything.** It is one product. Skynet optimizes DSPy
  programs and repositories. Do not describe DSPy and Optimize Anything as
  two separate paths or products, in code comments, docs, UI copy or
  marketing text.
- **DSPy programs** are one artifact type inside Optimize Anything. Skynet
  optimizes them with the DSPy optimizers. A prompt is the instruction of a
  DSPy program's predictor.
- **Engines.** The native engines come from the pinned upstream GEPA
  implementation and include GEPA, Best-of-N and Meta-Harness, plus Skynet's
  own AutoResearch and the pinned AutoSaddler and ShinkaEvolve packages.
  `Auto` composes eligible engines without redefining their algorithms.
- **Vercel Sandbox** is the production execution boundary for user workloads,
  including optimization setup, runs, recovery, and completed-run
  interactions. Workers are the control plane: they orchestrate jobs, reserve
  and settle budgets, broker scoped credentials, persist checkpoints, and
  dispatch sandbox work.
- **Protected execution** reserves wallet and run-budget headroom before paid
  work. Managed model usage, Skynet fees, and sandbox usage are metered. For
  BYOK model routes, the Skynet budget covers sandbox usage, with no model fee;
  the model provider charges the user's key directly.
