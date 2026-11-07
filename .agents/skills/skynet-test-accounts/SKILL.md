---
name: skynet-test-accounts
description: Use this skill before any signed-in check on Skynet shared staging or a PR preview (pr-<n>.staging.skynetml.com), for acceptance checks, UI evidence or API calls with a user token. It says which target to use, how to confirm the served commit with /api/version and /health, how agents read the pool accounts from AWS Secrets Manager and sign in with login.sh, the DNS-over-HTTPS fallback, and the rules for secrets and test data.
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
  It needs its own account.
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

## 4. Sign in with the staging account pool

The credentials for `pool-1` to `pool-4` are in AWS Secrets Manager:
`skynet/staging/test-accounts` (account 669242775630, region `us-east-2`,
profile `skynet`). It is a JSON object keyed by slot. Each slot has
`email`, `password`, `name`, `target`, `app`, `api` and `status`
(`verified` means the account can sign in).

- The orchestrator gives you one slot. Use only that slot. If you did not
  get a slot and your task needs a signed-in check, stop and report "no test
  account was handed over".
- Check your AWS login first: `aws sts get-caller-identity --profile skynet`.
  If it fails, report it. Do not run the sign-in.
- Read only your slot, inside your own script. Load it without printing, and
  sign in with `login.sh` in this folder. Set `SLOT` to your slot:

```bash
export SLOT="pool-<n>"   # your slot, for example pool-2
eval "$(aws secretsmanager get-secret-value --profile skynet --region us-east-2 \
  --secret-id skynet/staging/test-accounts --query SecretString --output text |
  python3 -c 'import json,os,shlex,sys;a=json.load(sys.stdin)[os.environ["SLOT"]];print("export SKYNET_POOL_EMAIL=%s SKYNET_POOL_PASSWORD=%s" % (shlex.quote(a["email"]), shlex.quote(a["password"])))')"
export PREFIX=SKYNET_POOL APP=https://staging.skynetml.com API=https://api-staging.skynetml.com
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

- The pool accounts exist on shared staging only. A PR preview has an empty
  database, so it needs its own account. If the orchestrator gives you one,
  export it as `SKYNET_PR_<n>_EMAIL` and `SKYNET_PR_<n>_PASSWORD`, set
  `PREFIX=SKYNET_PR_<n>` and the preview `APP` and `API`, and source
  `login.sh`. If you get none, record the signed-in check as a coverage gap.
- **Never sign up on shared staging or on a preview** for a normal check
  (see the exceptions below). **Do not ask Gilad to create an account.**
- When you finish, also after a failed check, tell the orchestrator, so it
  can give the slot to another agent.

### Sign-up exceptions

The sign-up ban covers normal checks. It has two exceptions:

1. **Acceptance checks of an authentication change** (sign-up, email
   verification, login). These need fresh accounts and real email codes, so
   a sign-up is part of the test. Do it only when the issue covers an auth
   change. Put the issue label in the account name, and record each account
   you create in the PR.
2. **Sign-ups by the orchestrator**: the setup of the staging pool
   accounts, and the preview merger, which signs up and runs a real
   optimization in each preview (`docs/agents/pr-previews.md`).

## 5. Secrets

- Read credentials only inside your own script (environment variables or the
  Secrets Manager secret). Never print, `cat` or echo them.
- Never put passwords, tokens, cookies or email codes in prompts, logs,
  screenshots, videos, commits, PR text or Linear.
- In evidence, name the account by slot (`pool-2`, `pr-827`) and
  by email domain only.
- In a browser, sign in through the normal login page. Keep the password out
  of recordings.
- A test-account login is access setup, not proof of email verification.
  Test authentication changes with fresh accounts and real codes (sign-up
  exception 1 in section 4).

## 6. Test data

- Slots keep their data between agents. Put your label (the issue or PR, for
  example `SKY-123`) in every name you create: projects, runs, files, keys.
- Do not depend on an empty account. Change or delete only data that your
  label made. Delete your fixtures when the PR's evidence does not need them.
- Create data through normal app flows when you can. Use realistic synthetic
  values, never real personal data.
- Never add funds, grant credits, change billing or change balances. Use only
  test allowances that already exist.
- Do not start expensive or scheduled runs only to see their UI. Follow the
  costly-workflow rule in the `skynet-pr-workflow` skill.
- Never borrow another agent's slot, Gilad's session or a personal account.
  Never create extra one-off accounts, except for the sign-up exceptions in
  section 4.

## 7. What to write in the PR

In the test-scope section, add:

- The target (staging or the `pr-<n>` preview), its URL, and the commit you
  confirmed.
- The slot you used. Never the email address or any secret.
- Each check you ran and its result.
- Gaps, for example "checked on staging, not on the preview" or "simulated
  state for the expensive run".
