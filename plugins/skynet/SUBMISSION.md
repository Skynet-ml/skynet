# Directory submission guide

This file holds the inputs for the three directory submissions of the Skynet
plugin. Research date: 2026-11-01. Check the forms again before you submit,
because the directories change their forms often.

| Submission | Where | What you submit |
| --- | --- | --- |
| A. Claude plugin directory | <https://claude.ai/directory/manage>, **Submit new**, **Plugin bundle** | This folder in the GitHub repository |
| B. Claude connectors directory | <https://claude.ai/directory/manage>, **Submit new**, **MCP connector** | The server URL `https://mcp.skynetml.com/mcp` |
| C. OpenAI plugin directory (ChatGPT and Codex) | <https://platform.openai.com/plugins>, **Upload new or existing plugin** | A ZIP of this folder |

One OpenAI submission covers ChatGPT and Codex. There is no separate Codex
directory.

## Blockers before any submission

1. **The server must be live.** `https://mcp.skynetml.com/mcp` (SKY-229)
   must serve the five tools with working OAuth. Both directories connect to
   it during review.
2. **Tool annotations.** Fix these in the server (SKY-229), not here:
   - Claude requires a `title` on every tool. The tools have none.
   - OpenAI requires explicit `readOnlyHint`, `destructiveHint` and
     `openWorldHint` booleans on every tool. The read tools lack
     `destructiveHint`. `start_run` and `stop_run` lack `openWorldHint`.
   - OpenAI marks a transaction that cannot be undone as destructive.
     `start_run` spends balance and has `destructiveHint: false`. A reviewer
     can flag it. Decide: set it to `true`, or keep `false` and write the
     justification below.
3. **Privacy policy.** OpenAI requires that the policy names every category
   of user data that the tools return. <https://skynetml.com/privacy> does
   not mention AI assistant connectors. Add a section: run specs, workspace
   code, test cases, results, run status and spend, and the balance go to
   the AI assistant that the user connects.
4. **OpenAI domain verification.** The portal gives a token. Serve it as
   plain text at `https://mcp.skynetml.com/.well-known/openai-apps-challenge`
   or at the same path on `skynetml.com`. This needs a small deploy.
5. **Reviewer account.** Both directories need a test account. OpenAI
   refuses accounts that need MFA, email or SMS codes, or magic links. The
   reviewer signs in with Google. Create a dedicated Google account and a
   Skynet account with a small balance and some finished runs. Turn off
   2-step verification on that Google account if Google allows it, or ask
   the OpenAI reviewers how they want to handle Google sign-in. Never put
   the credentials in this repository.
6. **Demo video.** OpenAI needs `demo_recording_url`: a video of the five
   positive test cases on the live server.

## Shared listing text

| Field | Value |
| --- | --- |
| Name | Skynet |
| Developer | Skynet (OpenAI replaces this with the verified organization name) |
| Website | <https://skynetml.com> |
| Support | <https://github.com/Skynet-ml/skynet/issues> |
| Privacy policy | <https://skynetml.com/privacy> (HTTP 200 on 2026-11-01) |
| Terms of service | <https://skynetml.com/terms> (HTTP 200 on 2026-11-01) |
| Documentation | <https://github.com/Skynet-ml/skynet/tree/main/plugins/skynet> (live after this folder reaches `main`) |
| Logo | `assets/logo.png`, 512 x 512 PNG, transparent background. Dark: `assets/logo-dark.png` |
| Brand colors | `#B4521F` (light), `#E0935A` (dark) |
| Category | OpenAI: `Developer Tools`. Claude: pick "Developer tools" or the nearest category in the portal list |
| Short description (30 characters or fewer) | Optimize DSPy programs |
| One-liner (200 characters or fewer) | Optimize DSPy programs against your own metric. Check a run spec before it runs, start it with a spending limit you approve, and get the best version with its score. |

Long description (OpenAI 4000 characters or fewer, Claude 2000 or fewer):

> Skynet improves a DSPy program against a metric that you write. You give
> the program in run.py, a few test cases and a score(input, output,
> expected) function. Skynet runs DSPy optimizers such as GEPA in a sandbox,
> scores each new version, and returns the best version with its score next
> to the starting score. Repository runs start from the Skynet app.
>
> With this connector, your assistant checks a run spec before it runs,
> suggests a spending limit, starts the run only after you agree to the
> limit, follows its progress, and shows the result. It can also list your
> runs and stop a run. A run spends at most the limit you set from your
> existing Skynet balance. Unused budget is released when the run ends. You
> need a Skynet account at skynetml.com.

Example prompts:

- Optimize this DSPy program with Skynet against my test cases.
- Show my recent Skynet runs.
- What is the status of my last Skynet run?

## Tools and annotations

| Tool | Suggested title | readOnlyHint | destructiveHint | openWorldHint | Scope | Justification |
| --- | --- | --- | --- | --- | --- | --- |
| `check_run` | Check run spec | true | false | false | `runs:read` | Validates a spec and reads the balance. Changes nothing. |
| `start_run` | Start optimization run | false | see blocker 2 | false | `runs:write` | Starts a job that spends at most the limit the user approves. Idempotent with the same key. |
| `get_run` | Get run | true | false | false | `runs:read` | Reads one run of the user's account, or a run shared with the user. |
| `list_runs` | List runs | true | false | false | `runs:read` | Reads the user's own runs. |
| `stop_run` | Stop run | false | true | false | `runs:write` | Cancels a run of the user's account, or a run shared with edit rights. The MCP tools cannot resume a stopped run. Some GEPA runs show Resume on the run page. |

`openWorldHint` is false for all tools, because they act only inside the
user's Skynet account.

## A. Claude plugin directory

Source: <https://claude.com/docs/plugins/submit> and
<https://claude.com/docs/plugins/pre-submission-checklist>.

- Who: a paid claude.ai plan. On Team or Enterprise, an Owner submits.
- Steps: Source, Validate, Listing details, Data handling, Compliance,
  Review and submit.
- Source: repository `Skynet-ml/skynet`, plugin path `plugins/skynet`,
  branch `main` (after promotion) or a tag. Your connected GitHub account
  needs push access. The repository is public.
- Listing details come from `.claude-plugin/plugin.json` and `README.md`.
- Data handling: the plugin has no local code. All data goes to the hosted
  server. Use "Data handling answers" below. The plugin form asks about
  personal data, other services, retention and users under 18.
- Compliance: a contact email, and four acknowledgements.
- Repository limits: under 50 MiB as an archive, fewer than 10,000 files
  and folders. Measured on 2026-11-01: 17.4 MB compressed archive, 2,218
  files and 245 folders. A subfolder plugin can be held if its files reach
  outside the folder. This plugin does not.
- Local check: `claude plugin validate plugins/skynet --strict` and
  `claude plugin validate . --strict` from the repository root.

## B. Claude connectors directory

Source: <https://claude.com/docs/connectors/building/submission> and
<https://claude.com/docs/connectors/building/authentication>.

Anthropic recommends a connector submission even when a plugin references
the server. Skills cannot be submitted alone; they go in plugin A.

| Step | Input |
| --- | --- |
| Connection | `https://mcp.skynetml.com/mcp` |
| Tools | The five tools. Every tool needs a `title` (blocker 2). |
| Listing | Name (100 characters or fewer), one-liner, description, 1 to 5 categories, documentation URL, privacy policy URL, support contact, icon, URL slug `skynet` (permanent after publishing) |
| Use cases | The five positive test cases below. Prerequisite: a Skynet account with a balance. Access: reads and writes |
| Company | Name and website `https://skynetml.com`, primary contact |
| Authentication | OAuth with DCR and CIMD (`oauth_dcr` / `oauth_cimd`). PKCE S256. Scopes `runs:read runs:write` |
| Data handling | See "Data handling answers" below |
| Test and launch | Reviewer account (blocker 5). Confirm that you ran every tool in MCP Inspector or as a custom connector |
| Compliance | Seven acknowledgements |

OAuth checks for the server team:

- Hosted Claude apps use the redirect `https://claude.ai/api/mcp/auth_callback`.
  Claude Code declares `http://localhost/callback` and
  `http://127.0.0.1/callback`. The server must accept both loopback hosts
  and ignore the port.
- An unauthenticated call returns 401 with
  `WWW-Authenticate: Bearer resource_metadata="..."`.
- A failed refresh returns `invalid_grant`.
- Discovery, registration and token endpoints answer in 10 seconds or
  less. Refresh token requests answer in 30 seconds or less.
- Anthropic's egress range is `160.79.104.0/21`, if a firewall needs it.

## C. OpenAI plugin directory

Source: <https://developers.openai.com/plugins/deploy/submission>,
<https://developers.openai.com/plugins/deploy/submission-errors>,
<https://developers.openai.com/plugins/plugin-guidelines>,
<https://developers.openai.com/plugins/build/plugins>.

1. Make the ZIP. It must hold exactly one plugin root:
   `cd plugins && zip -r skynet.zip skynet -x 'skynet/SUBMISSION.md'`.
   It must not hold test credentials or reviewer instructions.
2. In the portal, choose the verified Developer identity and upload the
   ZIP. The listing fields come from `.codex-plugin/plugin.json`
   (`interface`). The skill scan can take up to 2 hours.
3. **MCPs**, **Connect**: complete domain verification (blocker 4). Then
   complete OAuth and wait for the tool scan.
4. **Review details**: reviewer credentials (blocker 5), the demo video URL
   (blocker 6), release notes, and countries (empty list means all).
5. **Submit for review** and accept the attestations.
6. After approval, choose **Publish plugin** yourself.

The manifest already holds the five positive and three negative test cases
and `commerce: false`. Add `demo_recording_url` under
`extensions."com.openai".review` and `publication.release_notes` before you
make the ZIP, or enter them in the portal.

OAuth notes:

- The server advertises `authorization_response_iss_parameter_supported`
  and CIMD, so ChatGPT uses its stable redirect
  `https://chatgpt.com/connector_platform_oauth_redirect`. Codex uses a
  loopback redirect (not confirmed in the OpenAI docs). The server must
  accept any loopback port.
- If an Enterprise workspace restricts domains, OpenAI asks for the
  `openid` and `email` scopes and a UserInfo endpoint with
  `email_verified: true`. The docs call this optional.
- Do not add `headers` to `.mcp.json`. Public submission refuses them.
- After publishing, the server origin cannot change. A new origin needs a
  new plugin.

Policy notes:

- Commerce: the plugin sells nothing and cannot add funds. Users spend an
  existing balance under a limit they approve. The guidelines allow access
  to features of an existing paid account. Keep `commerce: false`.
- The listing text must not mention prices, trials or promotions.
- The name must not contain "MCP" or "Plugin". A reviewer can call a
  one-word name generic. "Skynet" is the brand name.
- Screenshots: none. They are only for plugins with custom UI.

Release notes (suggested): "First release. Check, start, follow, list and
stop Skynet optimization runs, with a spending limit the user approves."

## Data handling answers

- Data the tools receive: run specs (objective, starting version, test
  cases, workspace code, model choices) and run ids.
- Data the tools return: check results, the suggested limit, the spendable
  balance in cents, run status, spend, results and the best version.
- Shared runs: the assistant can receive data from runs that other users
  share with the signed-in user.
- Storage: Skynet stores runs and results in the user's account, the same
  as runs from the web app. Deletion follows the privacy policy.
- No payment data passes through the tools. The tools cannot add funds or
  change balances.
- Access is per user through OAuth. A token never has admin rights.
- API owner: Skynet owns and runs the API. It is not a partner or third
  party API.
- Other services: the tools send data only to the Skynet server. A run
  calls the model providers that the user picks in the spec.
- Retention: runs stay in the user's account. Copy the retention terms
  from the privacy policy. Confirm them with the owner before you answer.
- Users under 18: not intended for them. Check the terms of service age
  rule before you answer.
- Health data: not intended. Users can put any text in a spec, so state
  that Skynet does not ask for health data.
- Sponsored content: none.

## Test cases

The same cases are in `.codex-plugin/plugin.json` for OpenAI. Use them as
the Claude use cases too.

Positive:

1. "Check whether this Skynet run spec is valid and tell me what limit to
   set: a DSPy program that classifies support tickets, with 6 labeled
   cases and an exact-match metric." Expect `check_run` only. No run starts.
2. "Optimize the ticket classifier DSPy program with Skynet. I agree to a
   limit of 5 dollars." Expect `check_run`, then `start_run` with `max_cost_cents`
   500.
3. "How is my last Skynet run doing? Show the result if it is done." Expect
   `list_runs`, then `get_run`.
4. "List my Skynet runs that failed." Expect `list_runs` with status
   `failed`.
5. "Stop my running Skynet optimization." Expect `list_runs`, a
   confirmation, then `stop_run`.

Negative (no tool call):

1. "Add 50 dollars of credit to my Skynet account."
2. "What is the weather in Paris tomorrow?"
3. "Delete all my Skynet runs."

## Open questions

- Who submits: a personal claude.ai account (Pro or Max) or a Team
  Owner?
- Which branch or tag does the Claude directory track? `main` serves the
  documentation URL only after promotion.
- `start_run` `destructiveHint` (blocker 2).
- Which countries for the OpenAI publication?
