---
name: skynet-test-accounts
description: Use this skill before any signed-in check on Skynet shared staging or a PR preview (pr-<n>.staging.skynetml.com), for acceptance checks, UI evidence or API calls with a user token. It says which target to use, how to confirm the served commit with /api/version and /health, how agents create a fresh test account for each check with account.py, sign in with login.sh and delete the account after the check, the DNS-over-HTTPS fallback, and the rules for secrets and test data.
---

# Skynet test accounts and targets

Read this before you run any acceptance check on Skynet staging or on a PR
preview. The PR rules that use these checks are in the `skynet-pr-workflow`
skill.

## 1. Pick the right target

| Your check needs | Use |
| --- | --- |
| The PR's own code (new behavior) | The PR preview: `https://pr-<n>.staging.skynetml.com`, API `https://api-pr-<n>.staging.skynetml.com` |
| The current product (baseline checks, unrelated flows) | Shared staging: `https://staging.skynetml.com`, API `https://api-staging.skynetml.com` |

- Before and After evidence: capture both on the PR preview, the Before first.
  Use staging for the Before only when the preview cannot show the baseline.
  A visual-only change may use simulated local renders and needs no target.
  See section 5 of the `skynet-pr-workflow` skill.
- Shared staging runs merged code only. It does not hold your PR's change.
- A preview has its own empty database. Staging accounts do not exist there.
  Create the account on the target you test.
- Never test against production. Railway is deleted; never use it.

## 2. Confirm the served commit

Before you record evidence, check that each affected service serves the
commit you test. Both endpoints return JSON with a `commit` field:

```bash
DOH=(--doh-url https://cloudflare-dns.com/dns-query)
curl -s "${DOH[@]}" "$APP/api/version"   # frontend: {"commit": "<sha>"}
curl -s "${DOH[@]}" "$API/health"        # backend:  {"commit": "<sha>", "status": ...}
```

- Compare `commit` with the full SHA of the PR head (preview) or of
  `origin/staging` (staging).
- If the preview serves an older SHA, the deploy is not done. Wait for the
  `preview` workflow, or report it. Never record evidence on a stale
  deployment.
- An unchanged service may run an older SHA only with proof that it misses
  no relevant change.

## 3. DNS-over-HTTPS fallback

The DNS lookup for `skynetml.com` can time out on some networks. Add
`--doh-url https://cloudflare-dns.com/dns-query` to every `curl` call (the
sign-in script does this). For a browser, turn on secure DNS, or report the
failure.

## 4. Create a fresh account for each check

Test accounts are ephemeral. Each signed-in check creates its own account
and deletes it when the check ends, also after a failure. No check uses a
fixed, shared or long-lived account. A fixed account collects data from
many agents, and its access can get lost.

- Check your AWS login first: `aws sts get-caller-identity --profile skynet`.
  If it fails, report it and record the signed-in check as a coverage gap.
- Create the account on the target that you test. `--target` is `staging`
  or `pr-<n>`. `--label` is your issue or PR, for example `SKY-123`:

```bash
python3 .agents/skills/skynet-test-accounts/account.py create --target pr-<n> --label SKY-123
```

`account.py` first writes the secret `skynet/staging/check-account/<id>`
(account 669242775630, region `us-east-2`, profile `skynet`) with the email,
password, app, API and status `pending`. Then it signs up through the normal
app flow: `POST /api/register`, then it reads the 6-digit code from the owned
Resend receiving inbox, then `POST /api/email-verify/confirm`. Then it sets
the status to `ready`. It prints only HTTP status codes and the lines
`id=<id> status=pending` and `id=<id> status=ready`. Keep the id for the
delete step. If `create` fails or stops after the first line, the secret
stays `pending`: run the delete step for that id.

- Sign in with `login.sh`. `account.py env` prints the exports for it; use
  it only inside `eval`, never print its output:

```bash
eval "$(python3 .agents/skills/skynet-test-accounts/account.py env <id>)"
. .agents/skills/skynet-test-accounts/login.sh
```

`login.sh` reads `<PREFIX>_EMAIL` and `<PREFIX>_PASSWORD`, sets `TOKEN` (and
no other variable), and prints only the HTTP status. Export the variables
first, then source the script in bash or zsh. Do not put them on the same
line as the `.` command: the shell drops them after the script returns, and
the next API call fails. If `APP`, `API` or `PREFIX` is empty, the script
stops with an error and makes no request.

Expect `login check: HTTP 200`. Then call the API with the token in a header
that curl reads from stdin, so the token stays out of the process list:

```bash
printf 'authorization: Bearer %s\n' "$TOKEN" | curl -s --doh-url https://cloudflare-dns.com/dns-query -H @- "$API/account/experience"
```

**Never print `$TOKEN`**, and never put it in a command argument. If the check
is not 200, run the script once more. If it still fails, stop and report the
exact status.

### Delete the account when the check ends

1. Cancel the account's running optimizations, or wait until they end.
   The delete removes job rows but does not stop a running job or sandbox.
2. Run the delete, also after a failed check:

```bash
python3 .agents/skills/skynet-test-accounts/account.py delete <id>
```

It signs in first. A `pending` secret can belong to a verified account,
because `create` or an earlier `delete` can stop after the confirm. If
sign-in fails and the status is `pending`, it confirms the email, because
an unverified account cannot sign in. It uses the sign-up code in the
inbox. If that code fails, it waits out the 60-second resend cooldown and
asks for a new code. After a good confirm it sets the status to `ready` and
signs in again. Then it calls `POST $API/account/delete` with the password,
and force-deletes the secret. Expect `account delete: HTTP 200` and
`secret removed`. If it fails, it keeps the secret. Report the account id
to the orchestrator.

"No new email code arrived" does not prove that the account is gone. A
verified account gets no new code. Use `forget` only when `create` printed
no `register: HTTP 2xx` line, the preview database is gone, or an earlier
`delete` printed `account delete: HTTP 200` but kept the secret. A `delete`
of an id with no secret changes nothing and exits 0.

- `account.py list` prints the id, target, status and creation date of every
  check account that still has a secret. The orchestrator runs it after each
  batch and deletes the leftovers. A deleted secret can still show for about
  a minute.
- `account.py forget <id>` removes only the secret. Use it only when the
  account does not exist in the app: `register` never succeeded, the
  preview database is gone (PR closed or 48 hours idle), or an earlier
  `delete` printed `account delete: HTTP 200`. The `delete` error
  says when to use it. Never use `forget` while the account still exists.
- On a preview, the whole database is also dropped when the PR closes, or
  after 48 hours with no push. This is a backstop, not the delete step.
- **Do not ask Gilad to create an account.** Never borrow a personal
  session or another agent's account.

### What the delete does not remove

- The delete makes no call to an upstream provider. A Stripe customer (made
  only by a checkout) and an OpenRouter runtime key (made only when the
  environment sets `OPENROUTER_PROVISIONING_KEY`) stay at the provider.
  Do not start a checkout for a normal check.
- Billing, ledger and audit rows stay as anonymized records, by design.
- The confirmation email stays in the Resend receiving inbox.

### Credit on new accounts

- A new staging account gets no credit. A new preview account gets
  `PREVIEW_GRANT_CENTS` (see `docs/agents/pr-previews.md`). Run checks
  that need a paid run on the preview, or record the gap.
- Never add funds except through "Grant test credit" below.
- Staging keeps the setup intake for new accounts. Previews skip it.

### Grant test credit (staging only)

A staging check that needs a paid run can fund its own check account with
the grant command `python -m core.billing.grant_test_credit`. All of these
rules apply:

- Use it only for a check account you created for this check with
  `account.py`. Never grant on production, to real customers or to manual
  accounts.
- Amounts depend on the check. Before each grant, ask Gilad in chat for a
  specific dollar amount: the smallest amount the check needs, with a short
  cost estimate. Gilad approves that amount, or a larger one if he thinks
  the check needs more. Spend as little as possible.
- Grant only after Gilad approves that grant in chat. Put his approval text
  in `--approval`. The command writes it to the task log only. The account
  sees the ledger row, so the row holds only `Test grant: <reason>`.
- All grants on all accounts share one $50 cap for each UTC day, kept in the
  staging database. The command refuses a grant that passes it.
- The command refuses every grant on a PR preview. Each preview has its own
  database, so a preview grant would get its own $50 cap.
- Never use a raw wallet INSERT, Stripe or any other path.

The command refuses unless `APP_ENVIRONMENT` is `staging`, the database
host is the `skynet-staging` RDS instance, the account id has the
`account.py` staging shape, and the account has a user row. Amounts are
dollars with at most 2 decimals. A retry with the same `--idempotency-key`
credits nothing more. Use a new key for each approved grant, for example
`<account-id>-1`. The key has at most 54 characters. `--reason` has at most
243 characters.

Run it on staging as a one-off ECS task. It uses the migrate task
definition, which has the staging database URL and `APP_ENVIRONMENT`.
The command line holds no email and no secret:

```bash
export AWS_PROFILE=skynet AWS_REGION=us-east-2
CFG=$(aws ssm get-parameter --name /skynet/staging/deploy --query Parameter.Value --output text)
CLUSTER=$(jq -r .cluster <<<"$CFG")
SUBNETS=$(jq -r '.subnets | join(",")' <<<"$CFG")
SG=$(jq -r '.security_groups.migrate' <<<"$CFG")
PUBLIC_IP=$(jq -r 'if .assign_public_ip then "ENABLED" else "DISABLED" end' <<<"$CFG")
FAMILY=$(jq -r '.families.migrate' <<<"$CFG")
OVERRIDES=$(jq -nc --arg id "$ACCOUNT_ID" --arg usd "$DOLLARS" --arg reason "$REASON" \
  --arg approval "$APPROVAL" --arg key "$KEY" \
  '{containerOverrides: [{name: "migrate", command: ["python", "-m", "core.billing.grant_test_credit",
    "--account-id", $id, "--dollars", $usd, "--reason", $reason, "--approval", $approval,
    "--idempotency-key", $key]}]}')
ARN=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$FAMILY" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=$PUBLIC_IP}" \
  --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$ARN"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" --query 'tasks[0].containers[0].exitCode' --output text
aws logs tail "$(jq -r .migrate_log <<<"$CFG")" --log-stream-names "migrate/migrate/${ARN##*/}"
```

- Set `ACCOUNT_ID` to the id `account.py` printed, `DOLLARS` to the approved
  amount, `REASON` to the check, `APPROVAL` to Gilad's words, and `KEY` to a
  new key.
- Exit code `0` prints `Granted $X to <id>` or, on a retry, `Already
  granted`. Exit code `2` prints `Grant refused: <reason>` and writes
  nothing. Report a refusal; do not work around it.
- Exit code `1` prints `Grant failed: <error class>` and writes nothing. The
  command hides the error text, because it can hold the email. Report it.
- Exit code `2` with usage text and no `Grant refused:` means a missing or
  bad argument. Fix the command line; nothing was written.
- Record the grant (account id, dollars, key) in the PR test scope.
- A preview already gives new accounts `PREVIEW_GRANT_CENTS`. The grant
  command refuses previews. If a preview check needs more, ask the
  orchestrator.

### Authentication changes

A check of sign-up, email verification or login uses the same fresh
account flow. The sign-up and the real email code are part of that test.
Record in the PR that you used fresh accounts and real codes.

## 5. Secrets

- Read credentials only inside your own script (environment variables or the
  Secrets Manager secret). Never print, `cat` or echo them.
- Never put passwords, tokens, cookies or email codes in prompts, logs,
  screenshots, videos, commits, PR text or Linear.
- In evidence, name the account by its target and label (for example
  `pr-827 SKY-123 check account`) and by email domain only.
- In a browser, sign in through the normal login page. Keep the password out
  of recordings.
- A test-account login is access setup, not proof of email verification.
  Test authentication changes with fresh accounts and real codes (section 4).

## 6. Test data

- Your account starts empty and is deleted after the check. Put your label
  (the issue or PR, for example `SKY-123`) in every name you create:
  projects, runs, files, keys.
- Create data through normal app flows when you can. Use realistic synthetic
  values, never real personal data.
- Never add funds, grant credits, change billing or change balances. Use only
  test allowances that already exist. The one exception is the staging
  test-credit grant command ("Grant test credit" in section 4), under all of
  its rules.
- Do not start expensive or scheduled runs only to see their UI. Follow the
  costly-workflow rule in the `skynet-pr-workflow` skill.
- Never borrow another agent's account, Gilad's session or a personal
  account.

## 7. What to write in the PR

In the test-scope section, add:

- The target (staging or the `pr-<n>` preview), its URL, and the commit you
  confirmed.
- That you used a fresh check account, and that its delete returned
  HTTP 200. Never the email address or any secret.
- Each check you ran and its result.
- Gaps, for example "checked on staging, not on the preview" or "simulated
  state for the expensive run".
