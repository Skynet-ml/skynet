# Architecture and local setup

Reference facts for coding agents. The always-on rules are in `AGENTS.md`.

## Tech stack

- **Backend**: Python 3.11+, FastAPI, Pydantic, SQLAlchemy, Alembic, DSPy,
  pinned GEPA, LiteLLM/OpenRouter, Vercel Sandbox, PostgreSQL/pgvector, optional
  Redis, and Stripe.
- **Frontend**: Node 22, Next.js 16 App Router, React 19, TypeScript,
  Tailwind CSS 4, Radix/shadcn primitives, Framer Motion, and NextAuth.
- **Dependency management**: `uv` is authoritative for backend development and
  CI; compiled `requirements*.txt` files support the pip/Docker paths. The
  frontend uses npm and a committed lockfile.
- **Deployment**: AWS ECS Fargate backend/worker/frontend containers and a
  LiteLLM gateway (`infra/terraform`), and a pinned Vercel Container Registry
  sandbox image.

## Project layout

```text
├── backend/
│   ├── main.py                  FastAPI process entry point
│   ├── worker_main.py           Background worker entry point
│   ├── manage.py                Administrative CLI
│   ├── pyproject.toml           Python package, dependencies, Ruff/Pytest config
│   ├── uv.lock                  Authoritative resolved dependency graph
│   ├── requirements*.txt        Reproducible pip/Docker dependency paths
│   ├── Dockerfile               API/worker and native optimizer runtime image
│   ├── SANDBOX_RUNTIME.md       Pinned sandbox image build/publish contract
│   ├── alembic/                 PostgreSQL schema migrations
│   ├── sandbox-runtime/         Node harness runtime and lockfile
│   ├── load_tests/              Locust/SLO release-gate tooling
│   ├── core/
│   │   ├── api/
│   │   │   ├── app.py           App factory, middleware, and router wiring
│   │   │   ├── auth.py          Backend token verification
│   │   │   ├── rate_limit.py    Redis-backed/fail-open request limiting
│   │   │   ├── static/scalar/   Bundled offline API-reference assets
│   │   │   └── routers/         Account, billing, dataset, job, sandbox,
│   │   │                        sharing, telemetry, wizard, and workflow APIs
│   │   ├── billing/             Shared budgets, atomic reservations,
│   │   │                        metering, BYOK vault/bridge, model gateway,
│   │   │                        MCP broker, and Vercel reconciliation
│   │   ├── models/              Pydantic API/domain models
│   │   ├── notifications/       Preferences and webhook/chat notifications
│   │   ├── registry/            DSPy module and optimizer registration
│   │   ├── service_gateway/
│   │   │   ├── agents/          Code/generalist agent services
│   │   │   ├── datasets/        Dataset planning and profiling
│   │   │   ├── embedding_pipeline/ Conversation embedding and summarization
│   │   │   └── optimization/    DSPy workflow, metrics, artifacts, and limits
│   │   │       ├── blackbox/    Pinned Optimize Anything engines, harnesses,
│   │   │       │                scorers, sandbox broker, and remote evaluator
│   │   │       └── training_ground/ Benchmark/training adapters
│   │   ├── storage/             ORM, migrations, job/checkpoint/dataset stores
│   │   ├── telemetry/           Server-side telemetry events
│   │   └── worker/              Job orchestration, preflight, recovery,
│   │                            scoped relay, and Vercel DSPy dispatch
│   ├── tests/                   Integration, load, and release-gate tests
│   └── usage_guide/             Notebooks and API-client examples
│
├── frontend/
│   ├── package.json             Dev/build/typecheck/lint/unit commands
│   ├── next.config.ts           Standalone build, headers, image/bundle config
│   ├── public/                  Static assets and local font subsets
│   ├── scripts/                 Frontend-specific validation utilities
│   └── src/
│       ├── app/                 Thin App Router entry points and layouts
│       │   ├── (dashboard)/      Authenticated dashboard home
│       │   ├── api/              NextAuth and account-security proxy routes
│       │   ├── datasets/         Dataset list/detail/share routes
│       │   ├── optimizations/    Job detail route
│       │   ├── share/            Shared optimization route
│       │   ├── submit/           Optimization submission route
│       │   ├── tagger/           Tagging session and share routes
│       │   ├── explore/          Public dataset discovery
│       │   ├── storage/          Account storage management
│       │   ├── login/            Sign-in and account recovery UI
│       │   └── privacy/ terms/   Public legal pages
│       ├── features/             Product feature slices
│       │   ├── submit/           Submit wizard, drafts, substeps,
│       │   │                    preflight, budget UI, and workflow canvas
│       │   ├── optimizations/    Results, logs, compare, export, and serve UI
│       │   ├── dashboard/        Job list, analytics, and bulk actions
│       │   ├── datasets/         Dataset library and editor
│       │   ├── tagger/           Assisted tagging sessions
│       │   ├── storage/          Storage usage and quota controls
│       │   ├── explore/          Dataset discovery experience
│       │   ├── auth/ billing/ settings/
│       │   ├── agent-panel/ trajectory/
│       │   └── sidebar/ tutorial/
│       ├── shared/               Charts, hooks, layout, providers, types,
│       │                        API/auth/i18n utilities, and UI primitives
│       └── types/                App-wide ambient/shared TypeScript declarations
│
├── deploy/
│   └── litellm/                 Gateway image, config, callbacks, and local stack
├── .agents/skills/              Repo skills for coding agents (PR workflow, test accounts)
├── docs/                        Repository notes; agents/ holds agent reference files
├── i18n/                        Locale source catalogs, schema, and glossary
├── scripts/                     i18n generation, sample data,
│   └── agent/                   install (`just setup`) and health-report scripts
├── data/                        Repository datasets/fixtures
├── Justfile                     Canonical local task shortcuts
└── README.md                    Product setup and API usage
```

Backend tests are primarily co-located under `core/**/tests/`; do not assume
that `backend/tests/unit/` is the complete unit-test suite. FastAPI route files
under `core/api/routers/optimizations/` are split further by lifecycle concern.

## Key local URLs

- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000
- **API reference**: http://localhost:8000/reference
- **OpenAPI document**: http://localhost:8000/openapi.json

## Running locally

```bash
# Install backend development dependencies.
just install

# Run both processes, or use `just backend` and `just frontend` separately.
just dev
```

Direct equivalents are `cd backend && uv run python main.py` and
`cd frontend && npm run dev`. Turbopack requires `frontend/node_modules` to be
a real directory inside the current worktree. Do not symlink that directory to
a sibling checkout; run `npm ci` in the worktree instead.

## Validation

```bash
# Backend lint and credential-free core/unit tests.
just lint-backend
cd backend && uv run --extra dev pytest core/ tests/unit/ -v

# Frontend CI gates.
cd frontend && npm run typecheck && npm run lint && npm run test:unit

# Production frontend build and generated-catalog drift.
cd frontend && npm run build
just check-i18n
```

`backend/tests/test_llm_integration.py` requires a running server and paid model
credentials; it is not part of the credential-free PR gate. Billing changes
must also pass `core/billing/tests/test_budgets_postgres.py` against a real
PostgreSQL URL in `SKYNET_BUDGET_TEST_DB_URL`. Backend dependency/runtime
changes must keep `uv.lock` and the pip requirements files aligned and pass both
stable and preview Docker build paths.

## Persistence and infrastructure

- PostgreSQL is authoritative for jobs, accounts, datasets, shares, billing,
  budgets, checkpoints, attempts, and usage. Configure it with `REMOTE_DB_URL`.
- Alembic owns schema changes under `backend/alembic/versions/`.
- Redis is optional and configured with `REDIS_URL`; distributed throttles fail
  open when it is absent.
- User workloads must not receive provider or MCP secrets. The control plane
  brokers only the selected credentials/tools and meters protected work before
  dispatch.

## Authentication

- There is no auth-off mode. Hosted deployments support backend-verified local
  accounts plus optional Google and GitHub OAuth.
- The frontend and backend share `BACKEND_AUTH_SECRET`; backend identity is the
  user's email across local and OAuth providers.
- See both `.env.example` files before changing auth.

## Internationalization and direction

- English is the default locale. Hebrew, Arabic, and Persian render RTL; the
  remaining supported locales render LTR.
- `i18n/locales/*.json` and `i18n/schema.json` are the source catalogs. Run
  `python3 scripts/generate_i18n.py` after catalog changes and
  `just check-i18n` before committing.
- Preserve direction, alignment, Unicode text, and locale-aware formatting in
  every shared component; do not hard-code Hebrew-only layout assumptions.
