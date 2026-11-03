# Skynet for Mac

An Electron shell that runs the whole Skynet stack on your Mac. Jobs use the
coding CLIs you already sign in to (Claude Code, Codex CLI), not
API keys.

On launch the app:

1. Reads your login shell `PATH` so it finds Homebrew, nvm and `~/.local/bin` tools.
2. Checks Docker, then a coding CLI: installed, new enough, signed in, and not at its usage limit.
   Each failed check shows an onboarding screen with the command to fix it.
3. Stops any API or web server that a crashed earlier session left running (recorded in `stack.pids` by process group and start time),
   removes leftover `skynet.job` containers, then starts Postgres (`pgvector/pgvector:pg16`, container `skynet-local-postgres`, volume `skynet-local-pgdata`).
4. Builds the job sandbox image `skynet-local-sandbox:<bundle id>` from `backend/Dockerfile` once per packaged build
   (the bundle id is version, commit and build time) and removes the tags of older builds.
   It builds for `linux/amd64`, because the Dockerfile installs x64-only harness binaries. Docker Desktop runs it under Rosetta.
5. Installs Python packages with the bundled `uv`, runs `alembic upgrade head`, and starts the API.
6. Starts the bundled Next.js server and opens it in the window.

All services listen on `127.0.0.1` only (API 48100-48119, web 48120-48139, Postgres 48140-48159).
Children inherit only an allow-list from the app's environment, so the CLIs run on your subscription and no child can bill a provider:
`PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`, `TERM`, `TZ`, `CLAUDE_CODE_OAUTH_TOKEN` (a subscription login),
`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, the proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, upper and lower case),
`SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `CODEX_CA_CERTIFICATE`, the Docker daemon selectors
(`DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG`, `DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY`) and every `LC_*` and `XDG_*` variable.
All other variables (API keys, base URLs, profiles, `CLAUDE_CODE_USE_*`, `NODE_OPTIONS`, ...) are dropped.
The CLI checks add only `PATH`. The API, worker and web server get their settings (`APP_*`, the database URL, ports, secrets) as explicit values from the app, never from your shell.
Quitting stops the web server, the API, Postgres and every `skynet.job` sandbox container.

## Requirements

- macOS on Apple silicon. CI also builds an Intel (x64) app, but nobody has run it on Intel hardware yet.
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running, with "Use Rosetta for x86_64/amd64 emulation" turned on
- One coding CLI, signed in: `claude` 2.1.226 or newer, or `codex` 0.160.0 or newer
- For `codex`: a ChatGPT sign-in kept in `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`). Skynet reads that file, so a login kept only in the macOS Keychain does not count. Set `cli_auth_credentials_store = "file"` in `~/.codex/config.toml`, then run `codex login`.

## Install a downloaded build

The builds are ad-hoc signed and not notarized, so macOS quarantines a downloaded copy.
Open the `.dmg`, drag Skynet to Applications, then run:

```sh
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

The first launch takes several minutes: it pulls Postgres, builds the sandbox image and installs Python packages.

## Build from source

You need Node 22 and git. From the repository root:

```sh
cd desktop
npm ci
npm test
npm run build:mac
```

`build:mac` compiles the shell, builds the frontend (`next build`, standalone output) when
`frontend/.next/standalone` is missing, stages the tracked backend files, downloads the pinned
`uv` release and checks its SHA-256, then writes `dist/Skynet-arm64.dmg` and
`dist/mac-arm64/Skynet.app`, plus `dist/Skynet-x64.dmg` and `dist/mac/Skynet.app`. Set `SKYNET_DESKTOP_REBUILD_FRONTEND=1` to force a fresh frontend build.

Check the signature:

```sh
codesign -dv dist/mac-arm64/Skynet.app
```

A locally built app is not quarantined, so it opens without the `xattr` step.

To run without packaging:

```sh
npm run bundle:stack   # once, or after backend/frontend changes
npm start
```

For desktop work, `npm run dev` rebuilds on every save. The setup screen
reloads at once; a change in `src/main` or `src/preload` relaunches the app,
which stops and restarts the local stack. Quit an installed Skynet.app first:
both use the same data folder and ports.

## Where things live

| Path | Contents |
| --- | --- |
| `~/Library/Application Support/Skynet/secrets.json` | Postgres password and auth secrets for the local stack (mode 0600) |
| `~/Library/Application Support/Skynet/local/` | Local state for the backend, including `cli_limits.json` |
| `~/Library/Application Support/Skynet/venv/` | Python environment |
| `~/Library/Application Support/Skynet/runtime/` | The backend and frontend copied out of the app bundle |
| `~/Library/Application Support/Skynet/stack.pids` | Process groups of the running API and web server; removed on a clean quit |
| `~/Library/Application Support/Skynet/logs/` | `desktop.log`, `api.log`, `frontend.log`, `migrate.log`, `sandbox-build.log` |

Skynet does not use the macOS keychain for these secrets. An ad-hoc app gets a new signature on every rebuild or update, so keychain access asked for the login password each time and blocked Quit while the prompt was open. The secrets protect only services on 127.0.0.1, and any process that runs as you can already read the Postgres volume, so a 0600 file is enough here. Skynet keeps the data folder at mode 0700. If `secrets.json` is damaged, Skynet stops with its path; remove the file and open Skynet again.

Earlier builds kept the secrets in a keychain-encrypted `secrets.bin`. Skynet does not decrypt it. On the first launch of a new build, Skynet creates `secrets.json`, deletes `secrets.bin`, and sets the new Postgres password on the existing volume, so your data stays. You sign in to the local web app once more. The old keychain item "Skynet Safe Storage" is no longer used. You can delete it in Keychain Access.

To reset all local data, quit Skynet and run:

```sh
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

Skynet sets the Postgres password from `secrets.json` on every start, so removing only one of them also works.

## Testing onboarding

These variables are for development and tests:

| Variable | Effect |
| --- | --- |
| `SKYNET_DESKTOP_FAKE_CLI_DIR` | Look up `docker`, `claude` and `codex` only in this directory. Put small shell scripts there to trigger each onboarding screen. |
| `SKYNET_DESKTOP_DATA_DIR` | Use this directory instead of `~/Library/Application Support/Skynet`. Write `local/cli_limits.json` there to trigger the usage-limit screen. |
| `SKYNET_DESKTOP_LANG` | `en` or `he`. Also sets Chromium's language, so the web app gets the same `Accept-Language` |
| `SKYNET_DESKTOP_THEME` | `light` or `dark` |
| `SKYNET_DESKTOP_NO_UPDATE` | `1` turns off the update check |
| `SKYNET_DESKTOP_PG_VOLUME` | Use this Docker volume for Postgres instead of `skynet-local-pgdata`, so a test run does not touch your data. Only a plain volume name is accepted |

The version floors in `src/main/cli-detect.ts` copy `MIN_VERSIONS` from the backend CLI catalog. Change both together.

## Releases

Push a `desktop-v<version>` tag. `.github/workflows/desktop.yml` builds and publishes a draft GitHub release.
It notarizes only when the `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` and `CSC_LINK`
(Developer ID certificate) secrets exist. Auto-update needs a Developer ID signature, so ad-hoc builds
log the update error and keep running.
