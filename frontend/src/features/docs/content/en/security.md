This page describes how Skynet protects your account, your keys and your data. It also tells you how to report a security problem.

## Report a vulnerability

Report security problems privately. Do not open a public issue.

- On GitHub, open the Skynet repository and go to **Security** > **Report a vulnerability**.
- Or send an email to `security@skynetml.com`.

The team aims to acknowledge each report within five business days.

## Your account

### Sign-in methods

You can sign in with:

- An email address and a password.
- **Continue with Google** or **Continue with GitHub**.
- **Sign in with a passkey**. A passkey uses Face ID, Touch ID or a hardware key.

You can manage the providers that you use under **Connected accounts**.

### Password rules

A password must:

- Have at least 8 characters.
- Have at most 128 characters.
- Not contain your email address.

A new account must verify its email address with a code. A password reset also uses a code that Skynet sends by email.

Skynet locks sign-in for an email address for a period after repeated failed attempts.

### Two-step verification

Turn on two-step verification for more protection. Choose one method:

| Method                | How it works                                            |
| --------------------- | ------------------------------------------------------- |
| **Authenticator app** | Enter a 6-digit code from your authenticator app.       |
| **Email codes**       | Enter a 6-digit code that Skynet sends at each sign-in. |

Skynet also gives you recovery codes. Keep them in a safe place. Use one if you lose access to your method.

> [!NOTE]
> If you sign in with Google or GitHub, that provider manages your two-step verification.

## Where your code runs

User workloads run in isolated sandboxes. In the hosted product, Vercel Sandbox is the execution boundary. This covers setup checks, optimization runs, recovery and work on completed runs.

Skynet workers do not run your code. They are the control plane. They start jobs, reserve and settle budgets, give scoped credentials to sandboxes, and save checkpoints.

Before Skynet starts paid work, it reserves headroom in your wallet and in the run budget. See [Billing](/docs/concepts/billing).

In the [Mac app](/docs/guides/mac-app), jobs run in local Docker containers. All local services listen on `127.0.0.1` only.

## Provider keys (BYOK)

You can add your own model provider keys.

- Skynet encrypts each key at rest.
- Skynet never returns the full key through the API.
- Skynet keeps only the last four characters and a status: unverified, verified or invalid.
- When you enter a key, Skynet sends a small test request to check it.

Your provider bills model tokens to your key. Skynet charges no model fee. The run budget covers sandbox usage only.

Skynet also removes secrets from logs and error messages. It replaces them with `[REDACTED]`.

## API tokens

API tokens let scripts and tools call the Skynet API.

- Each user has at most one valid token. When you generate a new token, the old token stops working.
- Tokens start with `skyd_`.
- Skynet stores only a hash of the token.
- Skynet shows the full token only once. Copy and store the token at once.
- After that, Skynet shows only a masked form: `skyd_…` and the last four characters.

Send the token in the `Authorization` header:

```http
Authorization: Bearer skyd_...
```

To stop a token, click **Revoke token**. For the full API, read [API](/docs/api).

> [!WARNING]
> Treat the token like a password. Anyone with the token can act as you through the API. Do not commit it to a repository.

## Model data privacy

The **Model data privacy** setting controls what model providers can do with your data. It applies to optimizations that Skynet bills to your balance.

| Option             | Effect                                                                                      |
| ------------------ | ------------------------------------------------------------------------------------------- |
| **Allow all**      | Any provider, including providers that can train on or store your prompts.                  |
| **No training**    | Only providers that do not train on your prompts and do not keep them. This is the default. |
| **Zero retention** | Only endpoints with a zero-data-retention agreement. Some models can be unavailable.        |

> [!NOTE]
> BYOK calls through OpenRouter follow your OpenRouter account's privacy settings. Native provider BYOK calls follow that provider's data policy.

## Visibility of runs

> [!WARNING]
> New runs are **Public** by default. A public run appears in public search. Anyone can find it and browse its results.

To keep a run out of public search, set **Visibility** to **Private** when you submit it. You can change the visibility later in the share dialog. See [Sharing](/docs/guides/sharing).

New datasets are **Restricted**. Only you and the people you invite can open them, until you change the link access. See [Prepare a dataset](/docs/guides/datasets).

## Your data

Open the **Privacy & data** tab in **Settings** to manage your data.

### Export your data

Click **Export** under **Export my data**. The export holds all the data in your account. Choose one format: JSON, CSV, Excel, Pickle, Parquet or Feather.

### Delete your account

Click **Delete account**. Enter your current password and type your email address to confirm.

> [!WARNING]
> This permanently deletes your account and all its data. You cannot undo it.

### Usage analytics

The **Send anonymous usage analytics** setting turns anonymous product analytics on or off.

## Next steps

- Read [Limits](/docs/limits).
- Read the [FAQ](/docs/faq).
