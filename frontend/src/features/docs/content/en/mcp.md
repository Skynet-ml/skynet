The Skynet MCP server lets an AI agent use Skynet for you. Claude, ChatGPT, Codex, Cursor and other MCP clients can check a run setup, start runs, watch them and stop them. You approve the connection one time in your browser. The agent sets a spending limit for each run.

## Server URL

Every client uses the same URL:

```text
https://mcp.skynetml.com/mcp
```

The server uses Streamable HTTP. Sign-in uses OAuth 2.1. You do not copy a token or an API key into the client.

## Connect your client

Select your client. Follow its steps, then go to [Approve the connection](#approve-the-connection).

```skynet-mcp-clients

```

## Approve the connection

The first time the agent calls Skynet, your browser opens a Skynet page.

1. Sign in to Skynet, if you are not signed in.
2. Read the page. It shows the agent name, the site it returns to, and your account.
3. Read what the agent can do:
   - See your optimization runs and their results.
   - Start and stop optimization runs, paid from your balance.
4. Click **Allow** to connect. Click **Deny** to stop.

After you click **Allow**, the browser returns to the agent. The agent can now call the tools below.

> [!NOTE]
> The agent sets a spending limit for each run. It cannot add funds.

## Tools

| Tool        | What it does                                                                      | Access |
| ----------- | --------------------------------------------------------------------------------- | ------ |
| `check_run` | Checks a run setup without running it or charging you. Suggests a spending limit. | Read   |
| `start_run` | Starts a run with a spending limit.                                               | Write  |
| `get_run`   | Returns the status, the spending and, when the run ends, the result.              | Read   |
| `list_runs` | Lists your runs, newest first. Can filter by status.                              | Read   |
| `stop_run`  | Stops a run. A second call returns an error that says the run already ended.      | Write  |

Read access uses the `runs:read` scope. Write access uses the `runs:write` scope.

No tool adds funds, changes your balance or changes billing settings.

## How an agent runs an optimization

1. The agent calls `check_run` with the run setup. Skynet runs the static checks and returns:
   - The result of each check.
   - The lowest spending limit the run can start with.
   - A suggested spending limit. It is at least $5.00.
   - The models that have no known price.
   - The amount you can spend now.
2. The agent calls `start_run` with a spending limit (`max_cost_cents`, from 1 to 1,000,000 US cents). It can also send an `idempotency_key`, so that a retry does not start a second run.
3. If the setup check is still running, `start_run` returns `in_progress` and `retry_after_seconds`. The agent calls `start_run` again with the same input after that time.
4. The agent calls `get_run` until the run ends. The final statuses are `success`, `failed`, `cancelled` and `stopped`.

> [!TIP]
> Tell the agent your spending limit in your prompt, for example "Optimize this repository. Spend at most $10." The agent then uses that limit in `start_run`.

The run stops when it reaches its spending limit. Read [Billing](/docs/concepts/billing) for how Skynet charges a run.

## Security

- Each client gets its own grant. The client never sees your password.
- Sign-in uses PKCE (S256). The client registers itself with dynamic client registration or a client ID metadata document.
- Access tokens expire after 1 hour. The client renews them with a refresh token.
- A refresh token expires after 30 days without use. A grant lasts at most 90 days. Then you approve the connection again.
- Each refresh token works only one time. If someone uses an old refresh token again, Skynet ends the grant.
- The agent can only see and change your own runs.

Read [Security](/docs/security) for how Skynet protects your account and data.

## Disconnect

To disconnect an agent, remove the Skynet server from the client. Most clients then revoke their tokens. If a client does not revoke them, its refresh token expires after 30 days without use.

## Troubleshooting

| Problem                                 | What to do                                                               |
| --------------------------------------- | ------------------------------------------------------------------------ |
| The connection fails right away         | Check the server URL. It must be exactly `https://mcp.skynetml.com/mcp`. |
| "This agent can't connect"              | Start the connection again from the client.                              |
| "Too many attempts"                     | Wait some minutes, then try again.                                       |
| The client does not open a browser      | Update the client. It must support remote MCP servers with OAuth.        |
| `start_run` fails with a balance error  | Add funds in Skynet, or start the run with a lower spending limit.       |
| The tools stop working after some weeks | The grant expired. Remove the server and connect again.                  |

For direct HTTP access without an agent, read [API](/docs/api).
