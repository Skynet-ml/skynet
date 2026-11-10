The Skynet Mac app runs Skynet on your own computer. It sends model calls through a coding CLI that is signed in on your Mac, so your CLI plan pays for those calls. You can also link the app to your hosted Skynet account.

## Requirements

- A Mac with Apple silicon. Intel builds exist, but nobody tested them on Intel hardware.
- Docker Desktop, running, with **Use Rosetta for x86_64/amd64 emulation** turned on.
- One coding CLI, signed in:
  - Claude Code (`claude`) 2.1.226 or newer, or
  - Codex (`codex`) 0.160.0 or newer.

> [!WARNING]
> Codex must use a ChatGPT sign-in. A sign-in with an API key does not count. A login kept in the macOS Keychain does not count either.

### Set up Codex

Codex must keep its credentials in a file. Do these steps before the first launch:

1. Open `~/.codex/config.toml` and add this line:

```toml
cli_auth_credentials_store = "file"
```

2. Sign in with your ChatGPT account:

```bash
codex login
```

## Install the app

The builds are signed ad hoc and are not notarized. macOS blocks them until you remove the quarantine flag.

1. Open the `.dmg` file.
2. Drag **Skynet** to **Applications**.
3. Run this command in Terminal:

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. Open Skynet from **Applications**.

> [!NOTE]
> The first launch takes several minutes. The app builds a local sandbox image and prepares its database.

## What happens at launch

The app does these steps in order:

1. It reads the `PATH` from your login shell, so it can find Docker and your CLI.
2. It checks Docker and your coding CLI.
3. It starts a local Postgres database in Docker.
4. It builds the sandbox image `skynet-local-sandbox` for `linux/amd64`. Docker runs it under Rosetta.
5. It updates the database schema.
6. It starts the local API and the web app, and shows the web app in the window.

### Onboarding checks

The app shows a list of checks: **Docker**, **Coding CLI**, **Signed in**, **Usage limit**, **Local services** and **Account**. If a check fails, the app shows a screen that tells you what to do:

| Screen                     | What to do                                                         |
| -------------------------- | ------------------------------------------------------------------ |
| **Install Docker Desktop** | Install Docker Desktop.                                            |
| **Start Docker**           | Start Docker Desktop.                                              |
| **Install a coding CLI**   | Install Claude Code or Codex.                                      |
| **Update your coding CLI** | Update the CLI to the minimum version above.                       |
| **Sign in to your CLI**    | Sign in to the CLI in Terminal.                                    |
| **Usage limit reached**    | Wait for the reset time. New jobs can start again after the reset. |

Click **Check again** after you fix the problem. Click **Open logs** to see the log files. On the **Usage limit reached** screen, you can click **Open Skynet anyway**.

## Local runs

In the app, model calls go through the CLI on your Mac. The model menu shows this mode as **Local**. The hint says: "Your plan pays for the calls, so this model adds $0 to the run."

The CLI names in the app are **Claude Code** and **Codex**.

> [!NOTE]
> The app removes API keys from the environment of the processes it starts. So the CLI uses your subscription, not an API key.

### Run the proposer on your CLI

The setting **Run the proposer on my CLI** controls where the proposer agent runs. The choices are **Agent engine (default)** and **Off (sandboxed proposer)**.

**Proposer access** sets what the CLI can touch:

- **Workspace only** is the default. It relies on each CLI's own sandbox.
- **Full access** runs the CLI with your user privileges.

> [!WARNING]
> With **Full access**, the CLI can read, change and delete any file you can, and reach the network. Claude's acceptEdits mode does not isolate the network, even with **Workspace only**.

### Tagging in the app

AI tagging in the Mac app needs your own provider key. Choose a model under **Your own key**. See [Label data with the tagger](/docs/guides/tagger).

## Link to your Skynet account

You can use the app without an account. Click **Use without signing in** for that. To sync with your hosted account, link the app.

### Link this Mac

1. At the **Sign in to Skynet** step, the app opens the page **Link a Mac** in your default browser.
2. Sign in to Skynet in the browser if it asks.
3. Check the list under **When linked, this Mac can:**.
4. Click **Link this Mac**. The page shows "Returning to the Mac app…".

If the sign-in fails or you cancel, the app shows **Try again** and **Use without signing in**.

When linked, the Mac can:

- See your name and email.
- See your balance.
- Sync your runs with your account.
- Use hosted models, paid from your balance.
- Use your connected data sources.
- Search and browse the web through Skynet.

The link uses a secure sign-in flow (OAuth with PKCE). The app stores its tokens encrypted on your Mac. When you unlink, Skynet revokes the tokens.

### Link limits

| Limit                        | Value                             |
| ---------------------------- | --------------------------------- |
| Linked Macs for each account | 10                                |
| Access token lifetime        | 1 hour, then the app refreshes it |
| Link idle timeout            | 30 days without use               |
| Link maximum age             | 365 days                          |

After the idle timeout or the maximum age, link the Mac again.

### Link status

The app shows one of these states: **Local mode**, **Sign in to sync**, your account email when the Mac is linked, or **Linked (offline)**.

## Local services and data

All local services listen on `127.0.0.1` only:

| Service  | Port range     |
| -------- | -------------- |
| API      | 48100 to 48119 |
| Web app  | 48120 to 48139 |
| Postgres | 48140 to 48159 |

The app keeps its data in `~/Library/Application Support/Skynet`. The `logs/` folder holds `desktop.log`, `api.log`, `frontend.log`, `migrate.log` and `sandbox-build.log`.

When you quit the app, it stops the web app, the API, Postgres and all job containers.

### Reset the app

> [!WARNING]
> These commands delete all local data, including your local runs and datasets.

Quit the app first. Then run:

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## Next steps

- [Submit a run](/docs/guides/submit).
- Read about [billing](/docs/concepts/billing) for hosted models.
- Read [Security](/docs/security).
