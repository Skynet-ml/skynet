---
name: reel-skynet
description: Skynet-specific extension of the reel skill. Helpers that drive Skynet (a PR preview, AWS staging or local dev) in a reel flow (wait for the preview, confirm the deployed commit, sign up a throwaway account, build a Manual task, wait for the inline IDE, full screen, drive the coding agent) and the rules for the Skynet review page. Use when verifying or filming a Skynet PR.
---

# reel-skynet

Requires the general **reel** skill (`~/.agents/skills/reel/SKILL.md`): read it
first. This adds Skynet helpers you import into a reel flow.

## Fast path

```bash
cd <this repo checkout>
PR=715 node ~/.agents/skills/reel/bin/reel.mjs run tools/reel-skynet/examples/ide-smoke.mjs --out <scratchpad>/pr715
```

The example runs against local dev (`APP_URL`, default `http://localhost:3000`, with SMTP unset),
because `signup()` cannot pass the email code that previews and staging send. Copy `examples/ide-smoke.mjs` to your scratchpad, change the import path to this
file's absolute path, and replace the part after setup with what the PR must prove.

## Helpers (`tools/reel-skynet/skynet.mjs`)

| Helper | Does |
| --- | --- |
| `previewUrl(pr)` | `https://pr-NNN.staging.skynetml.com`, the PR preview (API `https://api-pr-NNN.staging.skynetml.com`). Film a PR here. |
| `waitForPreview(pr, { sha })` | Polls the preview's `/health` (backend) and `/api/version` (frontend) until both serve `sha` (without `sha`: until both answer). Returns `{ backend, frontend }`. Throws after 20 minutes. |
| `STAGING_URL` | `https://staging.skynetml.com`. Use it for checks after the merge. |
| `deployedSha(page)` | Opens `/login` and reads the "Source code" link (`.../tree/<sha>`): the commit the frontend was built from. Compare it to the PR head before filming. |
| `signup(page, { tag: "rec715" })` | Registers `rec715-<random>@example.com`, signs in, sets English, skips the intake. Works only on local dev with SMTP unset. Previews and staging send a real email code, so an `@example.com` account cannot sign in there: create a fresh check account with `.agents/skills/skynet-test-accounts/account.py` and delete it after the clip. New staging accounts start with $0 credit; only `-pr-N` previews give the signup grant. |
| `newManualTask(page, { prompt, csvPath })` | Wizard: Manual, objective, Prompt artifact, uploads the CSV (default `fixtures/tickets.csv`, 12 tickets). Ends on Inputs. |
| `toMetricAndRun(page)` | Continues to Metric & run and waits for the inline IDE's explorer to list run.py. Returns boot ms. |
| `ide(page)` / `ideToolbar(page)` | The IDE (inline section, or the full-screen dialog) / its header: the Check and Full screen icon buttons. Found through the `[data-ide-host]` workbench host. |
| `agentPanel(page)` / `agentInput(page)` | The coding agent chat docked on the right (~320px) / its "Ask the coding agent…" box. |
| `waitForAgentIdle(page)` | Waits for a turn to start (up to 10 s) and then for its Stop button to go away. Use after arriving (kickoff) or sending. |
| `askAgent(page, text)` | Types into the agent, sends, waits for idle. |
| `enterFullScreen(page)` / `exitFullScreen(page)` | Clicks "Full screen" / "Exit full screen" in the IDE header and waits out the ~220 ms zoom. Nothing reloads; the editor keeps its state. |

Useful raw selectors: `.monaco-workbench .part.sidebar` (explorer, ~190px),
`.part.editor`, `.explorer-folders-view .monaco-list-row`, the editor's text is
`.part.editor .view-lines`. The agent's "Opened metric.py:12" row is a button:
`agentPanel(page).getByRole("button", { name: /^Opened / })`. The Check results
strip sits under the header (`ide(page).getByText("No problems")`), and a hand
edit dims it with "Code or setup changed — re-check".

The page must be on the app before a helper runs: start with
`startReel({ url: previewUrl(pr) + "/login" })`. Against local dev use
`http://localhost:3000/login` (backend :8000).

## Rules

- Always run `waitForPreview` (with the PR head `sha`) and `deployedSha` first; a clip
  of the wrong commit is worthless. A preview updates a few minutes after each push.
- Use a fresh account per run: `signup` on local dev, or a check account from
  `.agents/skills/skynet-test-accounts/account.py` on a preview or staging. Run
  `account.py delete <id>` after the clip, also after a failure. Cancel the account's runs
  first: the delete does not stop a running job or sandbox. Other agents may test the
  same environment at the same time with their own accounts; never reuse or touch theirs.
- Never buy credit or open checkout. A new preview account gets the preview credit;
  a new staging account has $0. If a flow needs more credit, stop and report.
- Prefer role, label and placeholder selectors (as the helpers do); the wizard's
  English strings live in `i18n/locales/ui/en.json`.

## Review page

The user reviews clips at https://claude.ai/artifact/CSpCcabpgWedu5AFFQNMF9, built
from a local `review-reel.html`. Get the current version with the Artifact tool
(`action: "read"` on that URL; the result names the saved file), then use reel's
`add` / `remove` on that file and republish it to the same `url` with the printed
`files` map. Replace a PR's old clips (`remove --pr N`) before adding new ones.
