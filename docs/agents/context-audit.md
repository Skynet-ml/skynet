# Context audit: Claude Code compared with Factory Droid

Status: measured on 2026-11-10 in one Claude Code desktop session in this repo.
Purpose: find why Claude Code sessions compact more often than Droid sessions,
and list what to cut.

## 1. Summary

- A Claude Code session in this repo loads about **121k tokens before the
  first action**. About 60k of it is the desktop app's own tool schemas.
- The user controls about **25–30k tokens** of the rest. The biggest parts are
  unused MCP servers and connectors (about 12–18k), duplicated global
  instructions (about 8k), unused plugin skills (about 2k) and MCP server
  instructions (about 1.7k).
- Each PR task also reads two overlapping 20 KB policy files (about 11k
  tokens). One of them can defer to the other (about 5.5k saved per PR task).
- The repo files are small (about 13 KB loaded). This PR trims them only a
  little, because the large costs are outside the repo.
- Droid starts much smaller: 5 MCP servers, about 21 KB of instructions, no
  desktop-app tool schemas, and rolling compaction.

## 2. Method and coverage gaps

- Sizes come from the first turn of this session (system prompt, tool
  schemas, deferred tool names, `instructions`, skill listing, MCP
  instructions, hook output) and from `wc -c` on each file.
- Token estimates use about bytes ÷ 3.6 for English text and bytes ÷ 3 for
  tool names. These are estimates, not tokenizer counts.
- **Gap:** I did not count compactions across past sessions. Some
  transcripts are about 400 MB and the scan did not finish in time.
- **Gap:** I did not run a Droid session. The Droid side comes from the public
  Factory docs and from `~/.factory/`. Each Droid fact is marked
  DOCUMENTED or INFERRED.
- **Gap:** The desktop app's built-in tools (Artifact, Browser, iOS
  Simulator, Workflow and others) have no documented on/off switch that I
  could find. I list them, but I do not estimate a saving for them.
- No transcript content is copied here. Only sizes are reported.

## 3. Layers, sizes and actions

"Before" is the measured size. "After" is the size when all actions in this
report are applied. Savings are tokens on every session start, unless the row
says otherwise.

| Layer | Before | After | Purpose | Action | Reason | Est. saving |
| --- | --- | --- | --- | --- | --- | --- |
| System prompt (harness) | 35 KB | 35 KB | Claude Code rules | Keep | Not user-controlled | 0 |
| Loaded tool schemas (desktop built-ins) | 195 KB | 195 KB | Artifact (59 KB), Browser (32 KB), Workflow, Agent, iOS Simulator (8 KB), ScheduleWakeup and others | Keep (gap) | No known switch; iOS Simulator is unused in Skynet | 0 (see §7) |
| Deferred tool names | 62 KB, 1348 names | about 8–20 KB | Names of MCP and connector tools | Trim | 13 sources are unused in Skynet (see §6) | 12–18k |
| `~/.claude/CLAUDE.md` | 17.6 KB | 1.1 KB | Global rules | Trim | 9.4 KB duplicates CONVENTIONS; 6.4 KB workflow table; Skynet-only rules | 4.6k |
| `~/.config/ai-style/CONVENTIONS.md` | 21.7 KB | 14.4 KB | Shared rules (Claude and Codex) | Trim | Skynet-only sections; stale `dspy_as_a_service` §9 | 2.0k |
| `~/.claude/AXI.md` | 2.1 KB | 0 (on demand) | CLI design rules | Move | Needed only when you design a CLI | 0.6k |
| `~/.claude/RTK.md`, `AXI-TOOLS.md` | 1.8 KB | 1.8 KB | Tool hints | Keep | Small and used | 0 |
| `CLAUDE.local.md` (Skynet, gitignored) | 3.5 KB | 1.4 KB | Machine rules | Trim | Hexdrift rule is stale (the checkout is gone); third copy of the Memory block | 0.6k |
| Repo `CLAUDE.md` | 0.6 KB | 0.4 KB | Imports AGENTS.md | Trim (this PR) | Repeats the local-rules note in AGENTS.md | 0.06k |
| Repo `AGENTS.md` | 12.8 KB | 12.8 KB | Always-on repo rules | Keep | Devin and Codex read it; it is already lean | 0 |
| `frontend/AGENTS.md` | 0.7 KB | 0.7 KB | Frontend rules | Keep | Loads only for frontend files | 0 |
| `docs/agents/*.md` | 18.7 KB | 18.7 KB | Reference files | Keep | Load on demand only | 0 |
| Skill listing | 19 KB, 428 skills | about 12 KB | Names and descriptions | Trim | Sales, finance, marketing, media and similar plugin skills are unused here | about 2k |
| Repo skill descriptions | 1.0 KB | 0.6 KB | Two repo skills in the listing | Trim (this PR) | Long content lists; the trigger sentence is enough | 0.1k |
| MCP server instructions | 14.5 KB | about 8.5 KB | Server usage notes | Trim | deepwiki (long Devin tool list), cua-driver, rea, postman, vercel | 1.7k |
| SessionStart hook output | 7 KB | 7 KB | gh-axi dashboard, codedb warmup, notify | Keep | Useful state | 0 |
| OptMem `wake` output | about 24 KB | smaller | Memory tree | Trim (out of scope) | Compress old nodes with `memo nap` | not estimated |
| **Per PR task:** `PR_REVIEW.md` + `skynet-pr-workflow` skill | 41 KB read | about 21 KB | PR policy | Trim | The two files repeat the same sections | 5.5k per PR task |

Total for instructions (global and repo files that load at start): about
60 KB before, about 32 KB after (about 7.8k tokens).

## 4. What Droid puts in context

| Fact | Source |
| --- | --- |
| Droid loads `AGENTS.md` from the working directory up to the git root, and from `~/.factory`, `~/.agents` and `~/.agent`. | DOCUMENTED (docs.factory.com, AGENTS.md page) |
| Nested `AGENTS.md` files load lazily, when Droid works in that folder. | DOCUMENTED |
| Limits: 80k characters at start, 40k characters for each discovery. | DOCUMENTED |
| Droid also reads `CLAUDE.md` for compatibility. | DOCUMENTED |
| `@import` lines in `CLAUDE.md` expand in Droid. | UNCONFIRMED |
| All tools of an MCP server load unless `disabledTools` or `enabledTools` removes them. Removed tools cost 0 tokens. | DOCUMENTED (MCP page) |
| Some deferred tool loading exists (`ToolSearch`, `ConnectorSearch`). | INFERRED |
| Droid has about 27 built-in tools. | INFERRED |
| Skills load only name and description at start. | DOCUMENTED (skills page) |
| Only SessionStart, UserPromptSubmit and PostToolUse hooks add context. | DOCUMENTED (hooks reference) |
| Compaction is rolling and anchored to a message. It starts at a fill line and keeps a retained part. The default limit is model-dependent. | DOCUMENTED (settings page; factory.com/news/compressing-context) |
| Context window sizes per model. | UNKNOWN |

Local Droid setup (`~/.factory/`):

- `~/.factory/AGENTS.md` is 8.3 KB. With the repo `AGENTS.md` and
  `CLAUDE.md`, Droid starts with about 21 KB of instructions.
- `mcp.json` has 5 servers: digup, codedb (lazy mode), rea, context7 and
  canvas. Claude Code has about 40 servers and connectors in this session.
- All Droid hooks run a notify script that adds no context.
- No `compactionTokenLimit` is set. The repo has no `.factory/` folder.

Conclusion: Droid starts with about one third of the instruction text, far
fewer tool names and no desktop-app tool schemas. Rolling compaction also
keeps more recent context after each compaction.

## 5. Ranked wins

1. **Turn off unused MCP servers and connectors in Skynet sessions.**
   About 12–14k tokens (about 16–18k with Vercel). See §6.
2. **Apply the global instruction trims.** About 7.8k tokens on every session
   in every repo. See §7.
3. **Make `PR_REVIEW.md` defer to the repo skill for Skynet.** About 5.5k
   tokens per PR task. See §7.
4. **Turn off unused plugin skill groups for Skynet.** About 2k tokens. See §6.
5. **Remove MCP servers with long instructions that Skynet does not use.**
   About 1.7k tokens (part of win 1 when those servers go).
6. **Compress the OptMem tree** with `memo nap` on old nodes. Not estimated.
7. **Repo trims in this PR.** About 0.2k tokens. Small on purpose.

## 6. What to turn off for Skynet

Deferred tool names by source (unique names, bytes in context):

| Source | Names | Size | Used in Skynet? | Action |
| --- | --- | --- | --- | --- |
| ElevenLabs connector | 213 | 14.6 KB | No | Off |
| rea (local MCP) | 207 | 6.4 KB | Rarely | Off; turn on for reverse-engineering tasks |
| Vercel plugin | 250 | 12.0 KB | Sandbox operations only | Off by default; on for sandbox ops work |
| Linear connector (duplicate of local `linear`) | 70 | 4.1 KB | Duplicate | Off (keep local `linear`) |
| Microsoft 365 connector | 50 | 3.3 KB | No | Off |
| Todoist connector | 49 | 2.9 KB | No | Off |
| chrome-devtools plugin | 32 | 2.0 KB | Rarely | Off |
| cua-driver (local MCP) | 62 | 2.0 KB | No | Off |
| Gmail connector | 30 | 1.7 KB | No | Off |
| canvas (local MCP) | 47 | 1.6 KB | No | Off |
| postman (local MCP) | 41 | 1.3 KB | No | Off |
| Google Drive connector and `gdrive` MCP | 14 | 0.8 KB | No | Off |
| semgrep plugin | 11 | 0.5 KB | Rarely | Off |
| Google Calendar connector | 9 | 0.5 KB | No | Off |
| linear, codedb, digup, context7, aws-mcp, playwright, claude-in-chrome, deepwiki | — | — | Yes | Keep |

How to turn them off:

- **Connectors (claude.ai):** the desktop app can turn a connector off for
  one session (`set_session_connector_enabled`). A default for each project
  is not documented. Turn them off in the app's Connectors settings if you
  never use them in Code sessions.
- **Plugins from `~/.claude/settings.json`:** add a gitignored
  `.claude/settings.local.json` in the Skynet checkout. Project settings
  override user settings for `enabledPlugins`:

  ```json
  {
    "enabledPlugins": {
      "vercel@claude-plugins-official": false,
      "chrome-devtools-mcp@claude-plugins-official": false,
      "semgrep@claude-plugins-official": false,
      "higgsfield@higgsfield": false,
      "runway-api@runway-dev": false,
      "evo@evo-hq-evo": false,
      "01.-getting-started@scenario-skills": false,
      "02.-finding-a-direction@scenario-skills": false,
      "03.-images@scenario-skills": false,
      "04.-game-art-and-3d@scenario-skills": false,
      "05.-video-and-audio@scenario-skills": false,
      "06.-socials@scenario-skills": false,
      "07.-consistency-and-custom-models@scenario-skills": false,
      "08.-reviewing-and-organizing-output@scenario-skills": false,
      "09.-formats-and-placements@scenario-skills": false,
      "10.-workflows-and-apps@scenario-skills": false,
      "11.-troubleshooting@scenario-skills": false,
      "12.-team-administration@scenario-skills": false,
      "13.-image-model-families@scenario-skills": false,
      "14.-video-model-families@scenario-skills": false,
      "15.-audio-model-families@scenario-skills": false,
      "16.-3d-model-families@scenario-skills": false
    }
  }
  ```

  Merge these keys into the existing `settings.local.json`. Do not replace
  the file.
- **Desktop-app plugins** (sales, finance, marketing, productivity,
  product-management, engineering, data, design, figma, superset and others)
  do not appear in `~/.claude/settings.json`. Most of their MCP servers need
  sign-in or fail to connect, so they add no tool names. They still add about
  220 entries to the skill listing. Turn them off in the app's plugin
  settings. The exact switch is not confirmed.
- **User-scope MCP servers** (rea, cua-driver, canvas, postman, gdrive): use
  `/mcp` in a terminal `claude` session in the Skynet folder to disable them
  for this project, or move them from user scope to the projects that use
  them.

## 7. Global file changes (wait for Gilad's decision)

Do not apply these without Gilad's approval. The full unified diffs and the
proposed files are outside the repo, in the shared session notes folder:
`context-audit-global.diff`, `proposed-claude-CLAUDE.md`,
`proposed-CONVENTIONS.md` and `proposed-Skynet-CLAUDE.local.md`. They stay
out of this public repo because they quote private files.

### `~/.claude/CLAUDE.md` (17.6 KB → 1.1 KB)

- Remove "Independent RevEngine review rounds" and "Skynet agent startup and
  verification". `CONVENTIONS.md` has the same text, word for word.
- Remove "Pull request review requirements". It is Skynet-only. The repo
  `AGENTS.md` and the `skynet-pr-workflow` skill hold the same rules.
- Remove the Memory block. `CONVENTIONS.md` has the same text (two wording
  differences only).
- Replace "10. Available Workflows & Agents" (6.4 KB) with:

  > Before a task, check the skill listing for a workflow that fits (for
  > example `/implement`, `/review`, `/scout-and-plan`). If one fits,
  > suggest it before you start.

  The skill listing already shows each workflow with its description.
- Remove the `@AXI.md` import. Add: "When you design or change a CLI that
  agents call, read `~/.claude/AXI.md` first."
- Remove the four tail blocks (local code search, Last30Days, REA, Context7).
  `CONVENTIONS.md` has the same text. Note: these blocks have
  `<!-- name:start -->` markers, so an install script writes them. Change
  the script to write them to one file only, or they come back.

### `~/.config/ai-style/CONVENTIONS.md` (21.7 KB → 14.4 KB)

- Replace "Independent RevEngine review rounds" and "Skynet agent startup and
  verification" with:

  > In the Skynet repo, follow its `AGENTS.md` and the `skynet-pr-workflow`
  > and `skynet-test-accounts` skills. They hold the RevEngine review,
  > test-account, staging, UI-evidence and merge rules.

  The skill has each rule (Opus 5.5 at medium effort, fresh verify, rank,
  costly-workflow rule, simulated UI label, image and video specs).
- Remove "9. Backend Python Style (dspy_as_a_service)". The repo `AGENTS.md`
  has the same rules under "Commenting, docstring & import style". No
  `dspy_as_a_service` checkout exists in `~/PycharmProjects`.
- Remove "Parallel agents and UI proof". Move "How many agents at once" to
  the Skynet `CLAUDE.local.md`. The skill already has the before/after media
  rules.
- Codex reads this file too (`~/.codex/AGENTS.md` is a symlink). The repo
  `AGENTS.md` covers Codex in Skynet, so Codex loses no Skynet rule.

### Skynet `CLAUDE.local.md` (3.5 KB → 1.4 KB)

- Remove "RULE 0 — Fork isolation". The `Skynet.hexdrift` checkout no longer
  exists. Keep the port note (:8000, :3000, :3002).
- Add the "Parallel agents" rules from `CONVENTIONS.md`.
- Replace the full Memory block with: "OptMem uses a project store here. Add
  `MEMORY_DIR=~/.optmem/skynet` before every `~/.optmem/memo` command (wake,
  note, nap, recall, zoom). The other OptMem rules in the global conventions
  apply unchanged."

### `~/.config/ai-style/PR_REVIEW.md` (20.8 KB, read per PR task)

- These sections repeat the `skynet-pr-workflow` skill: required items for
  every PR, PR description structure, RevEngine, UI evidence, defect repairs,
  backend-only merges, Linear approval and the daily window.
- Recommended change: at the top, add "For Skynet PRs, the repo's
  `skynet-pr-workflow` skill is the full policy. Read it instead of this
  file." Keep this file only for rules the skill does not have, or for other
  repos. Then remove the "read PR_REVIEW.md before Skynet work" lines from
  the global files.
- No diff is given for this file, because the cut depends on whether other
  repos still use it.

## 8. Changes in this PR

- `CLAUDE.md`: remove the last paragraph. `AGENTS.md`, which `CLAUDE.md`
  imports, has the same rule about `CLAUDE.local.md` and `AGENTS.local.md`.
- `.agents/skills/skynet-pr-workflow/SKILL.md` and
  `.agents/skills/skynet-test-accounts/SKILL.md`: shorten the `description`
  line. The trigger sentence stays the same. The list of contents is shorter.
  The skill bodies do not change.
- This report.
