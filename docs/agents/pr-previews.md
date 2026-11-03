# PR preview environments

Reference for coding agents. The always-on rules are in `AGENTS.md`.

## What a preview is

Feature PRs target `staging`, never `main`. Each PR from this repository gets
a PR environment (preview), a full copy of staging with an empty database, at
`https://pr-<n>.staging.skynetml.com` (API `https://api-pr-<n>.staging.skynetml.com`).
The preview helper scripts require Bash 4 or newer.
Run the acceptance checks and record the videos there, before the merge.
`.github/workflows/preview.yml` builds it on every push and deletes it when the
PR closes. At most 4 previews run at once; a preview with no push for 48 hours
is deleted. PRs from forks or Dependabot, and PRs whose files cannot change
the running app (docs, `droid-wiki/`, `tools/reel-skynet/`, tests), get no
preview; add the label `preview` to ask for one.

Preview builds run the backend and frontend lanes in parallel. Backend
migration can overlap the frontend build, but backend and worker services roll
only after both image lanes succeed. The deploy step upserts all three
services; set `BACKEND_IMAGE` to the image used by the migration.

The remote MCP server of a preview is at
`https://api-pr-<n>.staging.skynetml.com/remote-mcp`. Staging uses
`https://mcp.staging.skynetml.com/mcp` and production
`https://mcp.skynetml.com/mcp`.

## Keep them able to run an optimization

The merger has to sign up and finish a real run in each `*-pr-<n>`
environment. Five things broke that once; keep them true:

- **The proxy knows the backend's key.** Previews share the `staging`
  `litellm`. If its database loses the virtual key in `LITELLM_PROXY_API_KEY`,
  every managed call (the assistant included) fails with
  `Invalid proxy server token`. `deploy/litellm/start.py` registers
  `LITELLM_SEED_KEY` at boot; on `staging` it must equal the backend's
  `LITELLM_PROXY_API_KEY`.
- **Preview accounts verify email and get credit.** In a `*-pr-<n>`
  environment, sign-up sends a real confirmation code, exactly like `staging`
  (the test-account helper reads it from the owned inbox), and a new account gets
  `PREVIEW_GRANT_CENTS` (from the `staging` app secrets, 1000 = $10)
  that can fund a budget. Production and `staging` keep a 0 grant.
- **Preview accounts skip the setup intake.** In a `*-pr-<n>` environment a
  new account (password or OAuth sign-up) starts with the intake marked done
  and default answers, so it lands straight in the app
  (`new_account_intake_completed_at` in `core/api/routers/experience.py`).
  To check a PR that changes the intake, open Settings → Account → Setup
  questions → "Run the setup again". Production and `staging` keep the intake.
- **The setup check routes the same models as the run.** An Auto run's
  payload names its models under `models.*`. `run_preflight` must alias them
  (`core/worker/unified_aliases.py`) before `ModelGateway.protect_payload`,
  exactly like the worker does. Otherwise the metric model gets no metered
  route, and the run fails with "Scorer models need a gateway the sandboxes
  can reach". Any new payload shape must go through the same aliasing in both
  places.
- **A sandbox command's kill timer stays at or under five hours.** A Vercel
  box may live 24 hours, but a process kill timer above 18,000,000 ms is
  rejected with `timeout should be <= 18000000`, and every run fails starting
  its model proxy. `Sandbox.run` caps `kill_after` at
  `_VERCEL_KILL_TIMER_CEILING_SECONDS`; never cap it at the box lifetime.
- **Diagnose before labeling `needs-human`.** Check each service's latest
  deployment and its events (`aws ecs describe-services --cluster
  skynet-<env> --services backend worker frontend litellm`; a preview's
  services are `pr-<n>-backend`, `pr-<n>-worker` and `pr-<n>-frontend` in
  `skynet-staging`, with logs in `/ecs/skynet-staging/preview`).
