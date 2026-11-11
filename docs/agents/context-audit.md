# Context audit: Claude Code compared with Factory Droid

Status: measured on 2026-11-10 in one Claude Code desktop session in this repo.
Purpose: find why Claude Code sessions compact more often than Droid sessions,
and list what to cut.

## 1. Summary

- A Claude Code session in this repo loads about **119k tokens before the
  first action**. About 54k of it is the desktop app's own tool schemas.
  About 6k of these go with one documented app switch. About 15k more
  can go with deny rules that I did not test in the desktop app (§6).
- The user controls about **24–28k tokens** of the rest. Unused MCP servers
  and connectors cost about 14–18k. Global and repo instruction trims save
  about 7.3k. Unused plugin skills cost about 2k, and MCP server instructions
  about 1.1k.
- Each PR task also reads two overlapping 20 KB policy files (about 11k
  tokens). One of them can defer to the other (about 5.5k saved per PR task).
- The repo files are small (about 13 KB loaded). This PR trims them only a
  little, because the large costs are outside the repo.
- Droid starts much smaller: 5 MCP servers, about 22 KB of instructions, no
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
  Factory docs and from the local Droid configuration files. I mark each Droid
  fact DOCUMENTED, INFERRED, UNCONFIRMED or UNKNOWN.
- **Gap:** I did not test the deny rules for the desktop app's tools
  (§6). The docs describe them for the CLI only. The one tested-by-docs
  switch is the app's Browser tools setting.
- **Gap:** Two measures of the loaded tool schemas differ. The first turn
  shows 195 KB. A later API request record shows 146 KB for 57 loaded
  tools. I did not find the cause. §6 uses the request record for the size
  of each tool group.
- This report copies no transcript content and no private file content. It
  reports only sizes and general descriptions.

## 3. Layers, sizes and actions

"Before" is the measured size. "After" is the size after you apply all actions in
this report. Savings are tokens on every session start, unless the row
says otherwise.

| Layer | Before | After | Purpose | Action | Reason | Est. saving |
| --- | --- | --- | --- | --- | --- | --- |
| System prompt (harness) | 35 KB | 35 KB | Claude Code rules | Keep | Not user-controlled | 0 |
| Loaded tool schemas (desktop built-ins) | 195 KB | up to about 76 KB less | Artifact, built-in browser, Workflow, iOS Simulator, visualize, Agent and others | Trim (partly) | Several groups are unused in Skynet (see §6) | about 6k; up to about 21k |
| Deferred tool names | 62 KB, 1348 names | about 8–20 KB | Names of MCP and connector tools | Trim | Many sources are unused in Skynet (see §6) | 14–18k |
| Global user instruction file | 17.6 KB | 1.1 KB | Global rules | Trim | 9.4 KB repeats the shared conventions file; 6.4 KB workflow table; Skynet-only rules | 4.6k, every repo |
| Shared conventions file (also read by Codex) | 21.7 KB | 16.5 KB | Shared rules | Trim | Skynet-only sections; a stale section for another project | 1.4k, every repo |
| CLI design rules file | 2.1 KB | 0 (on demand) | CLI design rules | Move | Needed only when you design a CLI | 0.6k, every repo |
| Tool hint files | 1.8 KB | 1.8 KB | Tool hints | Keep | Small and used | 0 |
| Machine-local rules file (gitignored) | 3.5 KB | 1.4 KB | Machine rules | Trim | A stale rule; a third copy of the memory rules | 0.6k, Skynet only |
| Repo `CLAUDE.md` | 0.6 KB | 0.4 KB | Imports AGENTS.md | Trim (this PR) | Repeats the local-rules note in AGENTS.md | 0.05k, Skynet only |
| Repo `AGENTS.md` | 12.8 KB | 12.8 KB | Always-on repo rules | Keep | Devin and Codex read it; it is already lean | 0 |
| `frontend/AGENTS.md` | 0.7 KB | 0.7 KB | Frontend rules | Keep | Loads only for frontend files | 0 |
| `docs/agents/*.md` | 18.7 KB | 30.9 KB | Reference files | Keep (this PR adds this report) | Load on demand only | 0 |
| Skill listing | 19 KB, 428 skills | about 12 KB | Names and descriptions | Trim | Sales, finance, marketing, media and similar plugin skills are unused here | about 2k |
| Repo skill descriptions | 1.0 KB | 0.6 KB | Two repo skills in the listing | Trim (this PR) | Long content lists; the trigger sentence is enough | 0.1k, Skynet only |
| MCP server instructions | 14.5 KB | about 10.5 KB | Server usage notes | Trim | Long notes from servers that Skynet does not use (deepwiki stays) | 1.1k |
| SessionStart hook output | 7 KB | 7 KB | GitHub dashboard, index warmup, notify | Keep | Useful state | 0 |
| Memory tool start output | about 24 KB | smaller | Memory tree | Trim (out of scope) | Compress old nodes | not estimated |
| **Per PR task:** per-PR policy file + `skynet-pr-workflow` skill | 41 KB read | about 21 KB | PR policy | Trim | The two files repeat the same sections | 5.5k per PR task |

Total for instructions (global and repo files that load at start): about
60 KB before, about 34 KB after. That is about 6.6k tokens saved in every
repo, plus about 0.65k in Skynet only (about 7.3k in Skynet).

## 4. What Droid puts in context

| Fact | Source |
| --- | --- |
| Droid loads `AGENTS.md` from the working directory up to the git root, and from the user's Factory, `.agents` and `.agent` folders. | DOCUMENTED (docs.factory.com, AGENTS.md page) |
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

Local Droid setup:

- The user-level Droid `AGENTS.md` is 8.3 KB. With the repo `AGENTS.md` and
  `CLAUDE.md`, Droid starts with about 22 KB of instructions.
- The Droid MCP configuration has 5 servers. Claude Code has about 40 servers
  and connectors in this session.
- All Droid hooks run a notify script that adds no context.
- The Droid settings set no `compactionTokenLimit`. The repo has no `.factory/` folder.

Conclusion: Droid starts with about one third of the instruction text, far
fewer tool names and no desktop-app tool schemas. Rolling compaction also
keeps more recent context after each compaction.

## 5. Ranked wins

1. **Turn off unused desktop tool groups.** About 6k tokens for the
   built-in browser (documented switch). Up to about 15k more with deny
   rules that need a test first. See §6.
2. **Turn off unused MCP servers and connectors in Skynet sessions.**
   About 14k tokens (about 18k with Vercel). See §6.
3. **Apply the global instruction trims.** About 6.6k tokens on every session
   in every repo, plus about 0.65k in Skynet. See §7.
4. **Make the per-PR policy file defer to the repo skill for Skynet.** About
   5.5k tokens per PR task. See §7.
5. **Turn off unused plugin skill groups for Skynet.** About 2k tokens. See §6.
6. **Remove MCP servers with long instructions that Skynet does not use.**
   About 1.1k tokens (part of win 2 when those servers go).
7. **Compress the memory tree** on old nodes. Not estimated.
8. **Repo trims in this PR.** About 0.16k tokens (repo `CLAUDE.md` and the two
   skill descriptions). Small on purpose.

## 6. What to turn off for Skynet

Deferred tool names by source (unique names, bytes in context):

| Source | Names | Size | Used in Skynet? | Action |
| --- | --- | --- | --- | --- |
| 6 personal-app connectors (email, calendar, office, tasks, file storage, voice media) | 365 | 23.8 KB | No | Off |
| Vercel plugin | 250 | 12.0 KB | Sandbox operations only | Off by default; on for sandbox ops work |
| 4 user-scope MCP servers for other work | 357 | 11.3 KB | Rarely or no | Off; on for tasks that need them |
| Linear connector (duplicate of the `linear` MCP server) | 70 | 4.1 KB | Duplicate | Off (keep `linear`) |
| chrome-devtools plugin | 32 | 2.0 KB | Rarely | Off |
| semgrep plugin | 11 | 0.5 KB | Rarely | Off |
| linear, codedb, digup, context7, aws-mcp, playwright, claude-in-chrome, deepwiki | — | — | Yes | Keep |

How to turn them off:

- **Connectors (claude.ai):** the desktop app can turn a connector off for
  one session (`set_session_connector_enabled`). I found no default for each
  project in the docs. Turn them off in the app's Connectors settings if you
  never use them in Code sessions.
- **Plugins from the user settings file:** merge these keys into
  `.claude/settings.local.json` in the Skynet checkout (gitignored; create
  it if it does not exist). Do not replace the file. Project settings
  override user settings for `enabledPlugins`. Set each unused plugin to
  false, for example `"<plugin>@<marketplace>": false`. Candidates: the
  vercel, chrome-devtools-mcp and semgrep plugins, and the media-generation
  plugins.
- **Desktop-app plugins** (sales, finance, marketing, productivity,
  product-management, engineering, data, design and others) do not appear in
  the user settings file. Most of their MCP servers need sign-in or fail to
  connect, so they add no tool names. They still add about 220 entries to
  the skill listing. Turn them off in the app's plugin settings. I did not
  confirm the exact switch.
- **User-scope MCP servers:** use `/mcp` in a terminal `claude` session in
  the Skynet folder to disable them for this project, or move them from user
  scope to the projects that use them.

### Desktop-app tool schemas

These tools load in full at every session start. Tool search does not
defer them. Sizes come from one API request record (bytes in the request).

| Tool group | Size | Est. tokens | Used in Skynet? | Switch | Status |
| --- | --- | --- | --- | --- | --- |
| Built-in browser (19 tools) | 22.4 KB | about 6.2k | No; UI checks use Chrome or a headless browser | Settings > Code > Browser tools: off | DOCUMENTED |
| Artifact | 37.1 KB | about 10.3k | No; UI evidence goes to Linear | Deny rule `"Artifact"` | DOCUMENTED for the CLI; not tested in the desktop app |
| iOS Simulator | 6.9 KB | about 1.9k | No | Deny rule `"mcp__Claude_Code_iOS_Simulator__*"`, or an app setting for mobile simulators | Deny rule as above; app setting UNCONFIRMED |
| Workflow | 5.5 KB | about 1.5k | Rarely; only when Gilad asks | Deny rule `"Workflow"` | As for Artifact |
| visualize (2 tools) | 4.2 KB | about 1.2k | Rarely | Deny rule `"mcp__visualize__*"` | As for Artifact |
| Session, plan, PR and terminal tools of the app | about 17 KB | about 4.7k | Yes | Keep | — |
| Other CLI built-ins (Bash, Read, Edit, Agent and others) | about 38 KB | about 10.6k | Yes | Keep | — |

- The docs say a bare tool name in `permissions.deny` removes the tool
  from context. A scoped rule only blocks calls. The desktop app reads
  `permissions` from the settings files.
- Put the deny rules in `.claude/settings.local.json` in the Skynet
  checkout. Merge them into the existing file; do not replace it.
- Test first: add the rules, start a new session, and check that the tools
  are gone. One public user report says a browser deny rule did not work
  in the desktop app.
- The Browser tools switch also turns off dev-server previews in the app.
- The app turns tool search on, but its own tools still load in full. I
  found no user switch that defers them.

## 7. Global file changes (wait for Gilad's decision)

Do not apply these without Gilad's approval. The full diffs and proposed
files are not in this repo. They are in this task's session notes, which only
Gilad can get. They stay out of this public repo because they quote private
files.

- **Global user instruction file (17.6 KB → 1.1 KB).** Most of its text
  repeats the shared conventions file word for word. A 6.4 KB workflow table
  repeats the skill listing. The Skynet-only rules already exist in the repo
  `AGENTS.md` and skills. An install script writes some blocks, so change the
  script too, or the blocks come back.
- **Shared conventions file (21.7 KB → 16.5 KB).** Replace the Skynet-only
  sections with one pointer to the repo `AGENTS.md` and skills. Remove a
  stale section for another project. Keep the generic PR review,
  parallel-agent and UI-evidence rules, because other repos use them. Codex
  reads this file too. The repo `AGENTS.md` covers Codex in Skynet, so Codex
  loses no Skynet rule.
- **CLI design rules file (2.1 KB → on demand).** Replace the import with one
  line: read the file when you design a CLI that agents call.
- **Machine-local rules file (3.5 KB → 1.4 KB).** Remove a stale rule and the
  third copy of the memory rules. Keep the local port notes.
- **Per-PR policy file (20.8 KB, read per PR task).** Many sections repeat
  the `skynet-pr-workflow` skill. Recommended change: at the top, add "For
  Skynet PRs, the repo's `skynet-pr-workflow` skill is the full policy. Read
  it instead of this file." This report gives no diff, because the cut
  depends on whether other repos still use it.

## 8. Changes in this PR

- `CLAUDE.md`: remove the last paragraph. `AGENTS.md`, which `CLAUDE.md`
  imports, has the same rule about `CLAUDE.local.md` and `AGENTS.local.md`.
- `.agents/skills/skynet-pr-workflow/SKILL.md` and
  `.agents/skills/skynet-test-accounts/SKILL.md`: shorten the `description`
  line. The trigger sentence stays the same. The list of contents is shorter.
  The skill bodies do not change.
- This report.
