# The Benchmark World

This document describes the simulated Skynet backend that the agent-harness
benchmark drives. It is the reference for what state exists, what each of the 49
tools does, and how to write a task `setup()` that shapes the world for a
scenario.

Everything here is **deterministic**: `now` is fixed at `2026-09-20T09:00:00Z`,
there is no wall-clock or random input anywhere, and every id, metric, timestamp,
and ledger entry is hard-coded in `bench/fixtures.py`. Two runs of the same task
produce byte-identical state.

---

## 1. How a call runs

`World` (in `bench/world.py`) holds one task's state in `world.s` (a deep copy of
`base_state()` plus any task setup) and a call log in `world.calls`.

`world.call(name, args)`:

1. Strips `None`-valued args (`{k: v for k, v in args.items() if v is not None}`),
   so an omitted optional argument and an explicit `null` are identical.
2. Appends a log record `{"tool", "args", "ok"}` **before** dispatch, so even a
   failed call is logged.
3. If `state["faults"][name]` exists with `times > 0`, decrements it and raises
   the injected `ToolError` (fault injection — see §9).
4. Dispatches to the registered handler `HANDLERS[name](world, args)`.
5. On success, records `ok=True` and the (deep-copied) result.
6. If the result is a dict with a dict `wizard_state` key, merges that patch into
   `world.s["wizard"]` — **always**, regardless of the tool's `mutates` flag.

`ToolError(status, detail)` renders to the agent as `HTTP <status>: <detail>`.

### The `mutates` flag and mutation checks

`@tool(name, mutates=True)` adds the tool to the `MUTATING` set. `checks.no_mutations()`
asserts that **no call in the log** is in `MUTATING` — it inspects the call log,
not a state diff. Consequences:

- Server-state-changing tools (job lifecycle, bulk ops, submits) are
  `mutates=True`.
- Wizard-patch tools (`update_wizard_state`, `stage_sample_dataset`,
  `profile_datasets`, `validate_datasets`, `set_column_roles`), all `request_*`
  UI-card tools, `update_user_preferences`, and every read-only tool are
  `mutates=False` — even when they append a UI card or set preferences — so they
  never trip a `no_mutations()` check. This mirrors the real client, where those
  are client-side or preference writes rather than backend optimization changes.

### Access & error model

Access is resolved per job (`_common.job_role` / `get_job`):

- **owner** — `job.username == caller` (case-insensitive) **or** the caller is an
  admin.
- otherwise the grant tier in `job["grants"][caller]` (`viewer`/`editor`/`owner`).
- otherwise no access.

`ROLE_RANK = {viewer: 1, editor: 2, owner: 3}`; a write needs `>= editor`.

Error codes (all raised as `ToolError`):

| status | meaning |
|--------|---------|
| 404 | unknown id, **or** a job the caller cannot see (existence is never leaked as 403) |
| 409 | wrong state for the operation (e.g. cancel a terminal job, results not ready) |
| 403 | a viewer attempting a write on a job shared with them |
| 422 | invalid / missing arguments |
| 402 | no spendable credits on a submit |

**Fidelity note (invented simplification):** submit routes charge **nothing** and
gate 402 **only** when `spendable_cents <= 0` (`paid_balance_cents +
free_grant.cents_remaining`). The real backend echoes cost estimates and does
not reject a submission by comparing an estimate against the balance, so the world
does not either.

---

## 2. State layout

`base_state()` returns these top-level keys:

| key | shape | notes |
|-----|-------|-------|
| `now` | str | `"2026-09-20T09:00:00Z"` |
| `user` | dict | the caller: `{"username": "dana", "is_admin": False}` |
| `users` | dict | `dana`, `noa` (both non-admin), `admin` (admin) |
| `jobs` | dict | 18 jobs keyed by `optimization_id` (see §4) |
| `datasets` | list | 5 library datasets (§5) |
| `samples` | list | 3 bundled Hebrew sample datasets (§5) |
| `staged` | dict | staged sample rows, keyed `staged-<sample_id>`; empty at start |
| `models` | list | 12 catalog models (§5) |
| `endpoints` | dict | discovery probe endpoints keyed by normalized base_url (§5) |
| `registry` | dict | `modules`, `metrics`, `optimizers` |
| `wallet` | dict | `paid_balance_cents`, `free_grant`, `ledger` (§5) |
| `search_corpus` | list | 16 public-gallery jobs (14 public + 2 private) (§5) |
| `blackbox` | dict | engine catalog + Auto-recipe availability (§5) |
| `tagging_sessions` | list | 3 sessions (§5) |
| `preferences` | dict | 7 client preference flags |
| `storage` | dict | `used_bytes`, `quota_bytes` |
| `wizard` | dict | submission wizard state; empty at start, grows via patches |
| `ui_cards` | list | chat UI cards appended by `request_*` tools; empty at start |
| `faults` | dict | fault-injection map; empty at start (§9) |
| `_seq` | int | lazily added; counter behind new job ids `22222222-…` |

### Users

`dana` is the caller (non-admin). `noa` is another non-admin user (owns jobs
shared with / hidden from dana). `admin` is an administrator (sees all jobs); no
task uses `admin` as the caller unless its `setup()` sets `world.s["user"]`.

---

## 3. Identifiers

- Seeded jobs: `00000000-0000-4000-8000-<n:012d>` for n = 1..18 (`_oid(n)`).
- Jobs added by a task's `setup()` via `add_job`: `11111111-0000-4000-8000-<count:012d>`.
- Jobs created at runtime by submit/clone: `22222222-0000-4000-8000-<seq:012d>`
  (`_seq` counter).
- Public-gallery jobs: `pub_0001`..`pub_0016`.

The three prefixes keep seeded, setup-added, and runtime-created jobs visually
distinct and collision-free.

---

## 4. Job table

All 18 seeded jobs are owned by `dana` unless noted. Metrics are
`baseline → optimized` test accuracy; `metric_improvement` = optimized − baseline
(auto-computed by `make_job`).

| id (`_oid`) | name | type | status | model | metrics | flags / notes |
|----|------|------|--------|-------|---------|---------------|
| 1 | support-tickets v2 | run | success | gpt-4o-mini | 0.60 → 0.80 | pinned; has result + serve + 10 per-example rows |
| 2 | support-tickets v2 (copy) | run | success | gpt-4o-mini | 0.62 → 0.79 | near-duplicate name; optimizer `dspy.teleprompt.MIPROv2` |
| 3 | ניתוח רגש בעברית | run | success | claude-haiku-4.5 | 0.5833 → 0.75 | Hebrew name; module `predict` |
| 4 | email-triage nightly | run | **failed** | gpt-4o-mini | — | metric `KeyError: 'label'` (missing column); diagnosable in logs; retryable |
| 5 | qa-bot tuning | run | **failed** | grok-4 | — | provider `HTTP 429` rate-limit; transient; `resumable=False` |
| 6 | long-run experiment | run | cancelled | gemini-2.5-pro | — | `resumable=True` (checkpoint retained) |
| 7 | live sentiment sweep | run | running | gpt-4o-mini | best_so_far 0.71 | `pausable=True`; `latest_metrics` live |
| 8 | paused-tuning | run | paused | claude-sonnet-4.5 | best_so_far 0.68 | `resumable=True` |
| 9 | queued-run | run | pending | gpt-4o-mini | — | no results yet |
| 10 | validating-run | run | validating | gpt-4o-mini | — | no results yet |
| 11 | grid: model bake-off | grid_search | success | (grid) | 0.50 → 0.80 | 6 pairs, 5 done + 1 failed (429); best pair `claude-haiku-4.5 + gpt-4o` (+0.30); pair 5 has no results |
| 12 | grid: reasoning effort | grid_search | success | (grid) | 0.55 → 0.70 | 4 pairs, all done; best `gpt-4o + claude-sonnet-4.5`; optimizer `dspy.teleprompt.BootstrapFewShot` |
| 13 | regression-risk run | run | success | gpt-4o-mini | 0.70 → 0.66 | **optimized WORSE than baseline** (negative improvement) |
| 14 | prompt-opt: cold email | blackbox | success | claude-sonnet-4.5 | 0.40 → 0.72 | `blackbox_result`; engine `gepa` |
| 15 | blackbox: scorer crash | blackbox | **failed** | claude-haiku-4.5 | — | scorer `ZeroDivisionError` in `metric_code`; engine `best_of_n` |
| 16 | noa shared classifier | run | success | claude-sonnet-4.5 | 0.60 → 0.77 | **owned by noa**, `grants={dana: viewer}` → dana can read, not write (403) |
| 17 | noa private classifier | run | success | gpt-4o | 0.58 → 0.79 | **owned by noa**, no grant → dana gets 404 |
| 18 | budget-capped run | run | stopped | gemini-2.5-pro | best_so_far 0.73 | `stop_reason=budget_reached`, `resumable=True`; `execution_budget` 50/50 |

Status coverage: pending, validating, running, success, failed, cancelled, paused,
stopped — all 8. Type coverage: run, grid_search, blackbox.

Jobs with stored `result` (→ `get_test_results`, `serve_info`): 1, 2, 3, 13, 16.
Grid jobs with `grid_result` (→ `get_grid_search_result`, pair reads): 11, 12.
Blackbox job with `blackbox_result`: 14.

---

## 5. Catalog data

### Datasets (`datasets`)

| id | name | rows | owner | notes |
|----|------|------|-------|-------|
| ds_support_tickets | support-tickets | 120 | dana | |
| ds_reviews_he | ביקורות לקוחות | 200 | dana | Hebrew name |
| ds_email_triage | email-triage | 90 | dana | |
| ds_tiny_eval | tiny-eval | 8 | dana | **too small** to split train/val/test |
| ds_noa_catalog | noa-product-catalog | 150 | noa | `shared_with={dana: viewer}` |

### Sample datasets (`samples`, bundled Hebrew catalog)

| sample_id | name | task_type | inputs → outputs | rows |
|-----------|------|-----------|------------------|------|
| sentiment-he | ניתוח רגש בעברית | classification | text → label | 12 |
| email-triage-he | מיון פניות בדוא״ל | classification | subject, body → category | 10 |
| qa-general-he | שאלות ותשובות כלליות | qa | question → answer | 12 |

Each sample carries `signature_code` and `metric_code`; staging one returns them
in the wizard patch.

### Models (`models`)

11 available OpenRouter models + 1 unavailable. Copy each entry's `name` verbatim
into submissions. Names: `openrouter/openai/gpt-4o-mini`, `.../gpt-4o`,
`.../gpt-5`, `openrouter/openai/o3-mini`, `openrouter/anthropic/claude-haiku-4.5`,
`.../claude-sonnet-4.5`, `.../claude-opus-4.5`, `openrouter/google/gemini-2.5-pro`,
`.../gemini-2.5-flash`, `openrouter/x-ai/grok-4`, `openrouter/deepseek/deepseek-chat`
(all `available: True`), and `openrouter/meta-llama/llama-4-scout`
(`available: False`, `needs_key: True`, unavailable reason set).

### Discovery endpoints (`endpoints`, for `discover_models`)

| base_url | behavior |
|----------|----------|
| `https://api.openai.com/v1` | `needs_key: True` — returns models only when an `api_key` is supplied, else the "requires an API key" error |
| `https://litellm.internal:4000` | open — returns 3 models |
| `https://broken.example.com` | `error: "HTTP 502"` — always errors |
| any other url | "Could not reach endpoint" error |

### Registry (`registry`)

`modules = [cot, flex, predict, react]`, `metrics = []`, `optimizers = [gepa]`.
(Submits validate `module_name` against `modules`; other optimizer names such as
`dspy.teleprompt.MIPROv2` appear on seeded jobs but are not gated on submit.)

### Wallet (`wallet`)

`paid_balance_cents = 2000`, `free_grant = {cents_remaining: 180, cents_total: 500}`,
so `spendable_cents = 2180`. 10 ledger entries (2 grants/top-ups of +500 & +1500,
6 run debits, +500 top-up, 1 more debit). **Invariant:** 2000 + 180 == sum of all
ledger `credits` deltas == 2180. `get_wallet` returns the ledger newest-first,
capped at 15.

### Public search corpus (`search_corpus`)

16 gallery jobs `pub_0001`..`pub_0016`. 14 are public; `pub_0015` (dana) and
`pub_0016` (noa) are `is_private: True` and are **never** returned by
`public_search`. Each has `name`, `task_name`, `module_name`, `optimizer_name`,
`winning_model`, `optimization_type`, `baseline_metric`, `optimized_metric`,
`summary_text`, `description`, `created_at`, `owner_username`. Owners across the
public set: maya, noa, ravid.

### Blackbox engines (`blackbox`)

5 engines: `gepa`, `best_of_n`, `autoresearch`, `meta_harness` (all
`available: True`) and `autosaddler` (`available: False`, deployment-disabled).
`auto_engines = [gepa, autoresearch, meta_harness]`, `auto_available: True`, so a
default/Auto-recipe blackbox submit is runnable. Naming `autosaddler` on a submit
raises 422.

### Tagging sessions (`tagging_sessions`)

`tag_0001` "support-tickets labeling" (dana, **pinned**), `tag_0002` "sentiment
gold set" (dana), `tag_0003` "noa triage set" (noa, `shared_with={dana: viewer}`).
`list_tagging_sessions` returns pinned first, then newest `updated_at`.

### Preferences (`preferences`)

`advanced_mode`, `expand_advanced`, `lite_mode` (bool), `wizard_code_assist`,
`wizard_split_mode` (str, default `"auto"`), `tagger_assist` (bool, True).
`update_user_preferences` merges these and rejects unknown keys (422).

---

## 6. Tools

All 49 tools, grouped by handler module. "M" = registered `mutates=True`.

### `jobs.py` — optimizations (23)

| tool | M | behavior | key errors |
|------|---|----------|------------|
| `list_jobs_optimizations_get` | | page of caller's jobs, newest first; filters status/username/type, `include_shared` | |
| `get_optimization_counts_optimizations_counts_get` | | totals grouped by status | |
| `get_job_summary_optimizations` | | compact card for one job | 404, 422 |
| `get_job_logs_optimizations` | | log lines, chronological; `level` filter | 404 |
| `get_test_results_optimizations` | | stored per-example baseline/optimized scores | 409 (no results) |
| `get_grid_search_result_optimizations` | | all pair results + `best_pair` | 409 (not grid / none) |
| `get_pair_test_results_optimizations` | | per-example results for one pair | 409, 422 (pair range) |
| `serve_info_serve` | | describe optimized program (no run) | 409 (not served) |
| `serve_pair_info_serve` | | describe one pair's program | 409 |
| `cancel_job_optimizations` | M | active → cancelled (resumable) | 409, 403 |
| `pause_job_optimizations` | M | running → paused | 409, 403 |
| `resume_job_optimizations` | M | paused/stopped+resumable → running | 409, 403 |
| `retry_job_optimizations` | M | failed/cancelled → pending | 409, 403 |
| `restart_job_optimizations` | M | terminal → pending (reset in place) | 409, 403 |
| `rename_job_optimizations` | M | set name | 422 (empty/>200), 403 |
| `toggle_pin_job_optimizations` | M | flip pinned | 403 |
| `clone_job_optimizations` | M | clone into 1–5 fresh pending runs (`{clones:[…]}`) | 422 (count) |
| `delete_job_optimizations` | M | hard-delete a terminal job | 409, 403 |
| `bulk_cancel_jobs_optimizations_bulk_cancel_post` | M | per-id cancel outcomes | |
| `bulk_delete_jobs_optimizations_bulk_delete_post` | M | per-id delete outcomes | |
| `bulk_pin_jobs_optimizations_bulk_pin_post` | M | pin/unpin 1–100 ids | 422 (count) |
| `submit_job_run_post` | M | queue a single run (needs module/optimizer/column_mapping/model_config + dataset source) | 422, 402 |
| `submit_grid_search_grid_search_post` | M | queue a model-pair sweep | 422, 402 |

Bulk ops return `{requested, succeeded, failed, results: [{optimization_id, ok, error?, status?}]}`.

### `analytics.py` — aggregates (3)

`get_analytics_summary_analytics_summary_get`, `get_model_stats_analytics_models_get`,
`get_optimizer_stats_analytics_optimizers_get`. All aggregate over the caller's
accessible jobs, with optional `optimizer` / `model` / `status` / `username`
filters. Averages use only successful runs' improvements.

### `account.py` — account reads + prefs (5)

| tool | M | behavior |
|------|---|----------|
| `get_wallet_for_agent` | | balance + free grant + `spendable_cents` + recent ledger |
| `list_models_for_agent` | | model catalog; optional `query` substring |
| `discover_models_models_discover_post` | | probe an endpoint; returns `{models, error}` (never raises for a bad endpoint) |
| `get_registry_snapshot_registry_get` | | sorted modules/metrics/optimizers |
| `update_user_preferences` | | merge preference flags (422 on unknown key) |

### `datasets.py` — datasets & staging (9)

| tool | M | behavior |
|------|---|----------|
| `list_datasets_for_agent` | | caller's datasets (+ shared); `is_owner` flag |
| `list_sample_datasets_datasets_samples_get` | | bundled sample catalog (no rows) |
| `stage_sample_dataset_datasets_samples` | | stage rows; returns `wizard_state` patch (staged id, column roles, signature/metric code, job name); 404 unknown |
| `profile_datasets_profile_post` | | profile + recommended split plan; 422 no rows / 404 bad staged id |
| `validate_datasets_validate_post` | | validate split fractions vs row_count; fractions must sum to 1.0 and each split ≥ 1 |
| `set_column_roles_datasets_column_roles_post` | | validate roles → `column_mapping` wizard patch; 422 unknown column/role/no input/output |
| `list_tagging_sessions_for_agent` | | sessions, pinned first then newest |
| `request_user_dataset_datasets_request_upload_post` | | append a dataset-upload UI card |
| `request_user_dataset_from_library` | | append a saved-dataset picker card |

### `wizard.py` — wizard, code validation, request cards (5)

| tool | M | behavior |
|------|---|----------|
| `update_wizard_state` | | returns a `wizard_state` patch of the given fields (422 on unknown field); the world merges it |
| `validate_code_validate_code_post` | | **AST-only** static check of `signature_code` (input/output fields) and `metric_code` (function present); never executes code |
| `request_code_authoring` | | append a code-authoring UI card |
| `request_user_inference` | | append an inference card (404 if job inaccessible) |
| `request_user_pair_inference` | | append a pair-inference card (404 / 409 not grid / 422 pair range) |

### `blackbox.py` — black-box optimization (3)

| tool | M | behavior |
|------|---|----------|
| `blackbox_engines_blackbox_engines_get` | | engine catalog + Auto availability; echoes `target` |
| `blackbox_scorer_dry_run_blackbox_scorer_dry_run_post` | | **AST-only** parse of the scorer's `metric_code`; `score` is always `null` (never executed); 422 if no scorer |
| `submit_blackbox_run_blackbox_run_post` | M | queue a blackbox run (needs `scorer` + `reflection_model_config`); rejects an unavailable engine (422); 402 on empty credits |

### `search.py` — public gallery (1)

`public_search_dashboard_search_post` — lexical search over **public** corpus jobs
only. Weighted term overlap (name×3, task×2, others×1). Filters: models,
optimizers, optimization_types, tasks, modules, date_from/date_to, owner_username,
shared_with_username. Sort `relevance` (default with a query) / `recent` / `gain`.
Paginated by `page` (1-based) / `size`. Each item is annotated with `gain` and
`relevance`.

---

## 7. Response shapes

Read tools return self-consistent, JSON-serializable projections that mirror the
real backend's field names (status enums, `metric_improvement`, split counts,
`best_pair`, etc.). Task **checks read STATE, not results** (`checks.dig` walks
`world.s`), so result shapes matter for agent realism, not for grading. Job
summaries project the `SUMMARY_FIELDS` tuple (`_common.py`) plus `role` and
`is_owner`.

---

## 8. Internal consistency invariants

These hold in `base_state()` and should be preserved by any `setup()`:

- `metric_improvement == round(optimized − baseline, 6)` for every job with both
  metrics (j13 is legitimately negative).
- Wallet: `paid_balance_cents + free_grant.cents_remaining == sum(ledger.credits)`.
- Grid `best_pair` is the pair with the max `optimized_test_metric`; the job's
  top-level metrics equal the best pair's; `total_pairs == completed + failed`.
- Status buckets in analytics/counts match the actual job statuses.

---

## 9. How to write `setup()`

A task's `setup(world)` runs after `base_state()` is loaded and the task's
`wizard_state` is applied, and **before** `freeze_initial()`. Use it to shape the
world for one scenario. Everything must stay deterministic.

### Add or override a job — `add_job(world, **overrides)`

```python
from bench.fixtures import add_job

def setup(world):
    # A fresh pending run owned by dana; id auto-assigned 11111111-…-0000
    add_job(world, name="my scenario run", status="pending",
            module_name="cot", optimizer_name="gepa",
            baseline_test_metric=0.5, optimized_test_metric=0.7)
```

`add_job` builds the job with `make_job` and stores it in `world.s["jobs"]`. It
assigns a deterministic id `11111111-0000-4000-8000-<current job count>` when you
don't pass `optimization_id`; pass one explicitly to pin it.

### Build a detached job dict — `make_job(**overrides)`

```python
from bench.fixtures import make_job

job = make_job(optimization_id="my-id", status="failed",
               message="…", stop_reason="error")
world.s["jobs"][job["optimization_id"]] = job
```

`make_job` starts from `_JOB_DEFAULTS` (a successful single `run` owned by dana on
gpt-4o-mini) and applies overrides. It auto-fills `metric_improvement` from the
two metrics unless you pass `metric_improvement` explicitly, and defaults
`column_mapping` to `text → label`. To attach results/serve blocks, set
`job["result"]`, `job["grid_result"]`, `job["serve"]`, or `job["blackbox_result"]`
after building.

### Shape existing state directly

`setup` may mutate `world.s` freely, e.g.:

- Change the caller: `world.s["user"] = {"username": "admin", "is_admin": True}`.
- Drain credits to force 402: set `paid_balance_cents` and
  `free_grant.cents_remaining` to 0.
- Grant/revoke access: set `world.s["jobs"][oid]["grants"] = {"dana": "editor"}`.
- Pre-seed the wizard: prefer the task's `wizard_state` field, or write
  `world.s["wizard"]`.

### Inject a fault — `state["faults"]`

```python
world.s["faults"]["submit_job_run_post"] = {"times": 1, "status": 503,
                                            "detail": "Service temporarily unavailable"}
```

The next `times` calls to that tool raise `ToolError(status, detail)` before the
handler runs; the counter decrements each time, so the tool recovers afterward.
Useful for retry / transient-failure scenarios.

---

## 10. Running the self-test

```
python -m bench.selftest
```

Asserts every `tools.json` tool has a handler (and none is orphaned), calls every
tool on a success path, exercises a representative error/lifecycle path per group
(404/409/403/422/402), and checks every result is JSON-serializable. Prints a
one-line PASS/FAIL summary and exits non-zero on failure. `fastmcp` is **not**
required for the self-test (it does not import `bench.server`).
