# Agent harness benchmark

A small benchmark of realistic Skynet-assistant tasks, used to compare agent
harnesses running the same model over the same tools.

## How it works

* `tools.json` holds the 49 tool schemas the production generalist agent can
  reach, dumped from the real FastMCP mount. Every harness sees exactly these.
* `bench/handlers/` implements those tools over an in-memory "world"
  (`bench/fixtures.py` is the starting state). Nothing touches the real backend,
  so runs are reproducible, free of side effects and need no database.
* A task (`bench/tasks/*.py`) is a user request plus deterministic checks over
  the final world state, the tool-call log and the reply. Each task carries an
  `oracle` that solves it through the same tool API; `bench.validate` proves
  every task is solvable and that doing nothing does not pass.
  The 34 tasks are split over six categories, six tasks each except `cross` (four):

  | Category | Module | What it covers |
  |---|---|---|
  | `setup` | `run_setup.py` | wizard edits, staging data, column roles, submitting runs and grids |
  | `insight` | `job_insight.py` | read-only questions about runs, failures, grids and regressions |
  | `lifecycle` | `job_lifecycle.py` | rename, cancel, resume, delete, and asking before destructive actions |
  | `bbserve` | `blackbox_serving.py` | black-box engines, code validation, serving a finished program |
  | `robust` | `account_robustness.py` | wallet, preferences, public search, tool faults, prompt injection |
  | `cross` | `cross_cutting.py` | multi-turn follow-ups, black-box launch, code-authoring card, model catalog |

* `bench.server` serves one task's world over MCP (`/mcp`) and a plain JSON API.
  The runner starts one server per attempt, so attempts never share state.
* Every harness gets the same system prompt (`brief.md`), the same user
  message, all tools at once and auto-approved tool calls. Built-in coding
  tools (shell, file edits, web) are disabled so only the Skynet tools count.

## Harnesses

| name | what runs |
|---|---|
| `dspy-reactv2` | the project's `RetryingReActV2` on DSPy's stock text prompt layout |
| `dspy-reactv2-stable` | the same loop under `StableRosterChatAdapter`: same text tool protocol, tool roster pinned to the first user message. This is what the platform's served and optimized ReAct agents run |
| `dspy-react` | stock classic `dspy.ReAct` |

SKY-327 removed `dspy-reactv2-fixed` (`ConversationReAct`): every Skynet agent now runs on Pi. Its results below are history.
| `codex` | `codex exec` with an OpenRouter provider |
| `opencode` | `opencode run` with a tools-only agent |
| `pi` | `pi -p`; Pi has no MCP client, so a small extension bridges the JSON API |

The CLIs must be installed and on `PATH`. The OpenRouter key is read from
`OPENROUTER_API_KEY` or `backend/.env`.

## Commands

Run from this directory with the backend's virtualenv:

```bash
PY=../../backend/.venv/bin/python
$PY -m bench.selftest                 # the simulated world behaves
$PY -m bench.validate                 # every task is solvable and non-trivial
$PY -m bench.run --harness pi codex --trials 2 --out results/main
$PY -m bench.report results/main
```

`bench.run` is resumable: attempts already in `results.jsonl` are skipped, and
attempts that hit an infrastructure error are retried.

`$PY -m bench.language results/main` reports how often each harness answered
in the wrong language. `--pin-language` tells `dspy-reactv2` and
`dspy-reactv2-stable` which language to reply in, so the two can be compared
without language drift deciding the outcome. `TASKS.md` lists every task with its prompt and checks.

## Results (2026-09-20, fixed loop added 2026-09-21)

`deepseek/deepseek-v4.1-flash` through OpenRouter, 36 tasks, 2 trials, 72
attempts per harness. The full report is `results/main/REPORT.md`.

| harness | pass | median time | tool calls | list cost / 1k tasks | English prompts answered in Hebrew |
|---|---|---|---|---|---|
| dspy-react | 97% | 44s | 2.1 | $5.83 | 0/60 |
| dspy-reactv2-fixed | 97% | 34s | 3.1 | $3.53 | 0/60 |
| opencode | 96% | 27s | 2.3 | $6.01 | 11/60 |
| codex | 94% | 45s | 7.6 | $6.96 | 5/60 |
| dspy-reactv2 | 92% | 26s | 2.1 | $11.07 | 12/60 |
| pi | 90% | 21s | 2.4 | $5.46 | 16/60 |

Reading the table:

- **The top three are tied.** With 72 attempts, a pass rate near 95% carries
  roughly five points of noise either way, so the model matters more than the
  loop on these tasks.
- **Language drift is the largest single failure cause.** 13 of the 22 failed
  attempts are correct answers written in Hebrew to an English prompt, pulled
  there by Hebrew data in tool results. `dspy.ReAct` never drifted. Production
  pins the reply language explicitly, which this benchmark does not.
- **`dspy-reactv2` costs about twice the others** because almost none of its
  input is served from the prompt cache (about 9k cached of 79k input tokens,
  against 30k to 120k for the rest).
- **`dspy-reactv2-fixed` removes both problems.** The old loop rebuilt its
  first user message on every step (tool roster and format reminder moved
  behind the history), so the prompt prefix changed right after the system
  message. The fixed loop sends tools on the provider's native channel and only
  ever appends messages. Cached input rose from 11% to 84% of all input tokens
  and list cost fell from $11.07 to $3.53 per 1k tasks, the lowest here. The
  remaining misses come from OpenRouter switching providers between calls,
  which every harness shares. With the reply language pinned it never drifted,
  and its pass rate joined the tied group. Its two failures were one dry-run
  task where it fixed the scorer and submitted anyway, and one ready run it
  declined to submit.
- **Substantive failures differ by harness.** Both DSPy loops submitted a run
  they were asked only to dry-run. Codex mishandled a one-field preference
  update in both trials. Pi missed several answer details.
- **Codex is the slowest and most token-hungry** at about 10 model calls per
  task against 3 to 4 elsewhere.

### Text-mode prompt layout (2026-09-21)

`dspy-reactv2-fixed` needs native tool calling, which the platform keeps off by
default because not every model a user picks supports it. `dspy-reactv2-stable`
is the fix for the text protocol: it changes only where the tool roster sits.
Both runs below are 36 tasks × 2 trials, the two variants back to back.

| run | harness | pass | median time | cached input | list cost / 1k tasks |
|---|---|---|---|---|---|
| `results/text_stable_pinned` (`--pin-language`) | dspy-reactv2 | 71/72 | 18s | 16% | $9.93 |
| | dspy-reactv2-stable | 71/72 | 12s | 73% | $3.45 |
| `results/text_stable` (no pin) | dspy-reactv2 | 71/72 | 26s | 36% | $8.14 |
| | dspy-reactv2-stable | 63/72 | 20s | 54% | $5.73 |

- **With the reply language pinned the two are identical on quality** (the same
  single failure, the dry-run task every loop struggles with) and the stable
  layout costs about a third as much and answers faster.
- **Without the pin the stable layout passed 8 fewer attempts.** Both variants
  drifted to Hebrew equally often (17/59 and 14/60 English prompts), but the
  stable run drifted on tasks whose checks look for English phrases: 6 of its 9
  failures are correct answers in Hebrew, against 1 for the stock layout. Read
  it as one more reason to pin the reply language, which every platform agent
  does, and as a caution that an unpinned loop may be more exposed.

One grader fix was applied after the run: the `insight-failed-count` check
rejected "3 of your 16 runs have failed". Its answer-only regex was corrected
and the three affected attempts were regraded from their stored answers.

The billed column in the report is biased by OpenRouter peak-hour pricing and
by billing-counter lag between batches; the list cost is computed from tokens.

## Limits

Tasks are single-turn (earlier turns are given as a transcript, except to
`dspy-reactv2-fixed`, which replays them as native history). That variant also
gets its reply language pinned from the prompt's language, standing in for the
UI locale the app pins from; the other harnesses get no such pin, so its drift
figure is not like for like. The backend is
simulated, so latency and failure behaviour of real routes is not measured.
Approval cards, tool phasing and streaming, which the production agent adds
around its loop, are out of scope: this measures the loop and the model.
The DSPy harnesses run with `max_iters=15` where production uses 8. Pi reaches
the tools through a small extension bridge rather than MCP. Harnesses report
cached tokens differently, so compare list cost before cached-token counts.
The suite is near its ceiling for this model, so it separates harnesses on
cost, latency and failure style more than on pass rate.
