This page answers common questions about Skynet. Each answer links to the page with the full details.

## General

### What is Skynet?

Skynet is a platform that optimizes repositories against your own data and scorer. You upload a dataset, describe the task, and start a run. The run tries new versions and scores them on held-out data. To start, read the [Quickstart](/docs/quickstart).

### What can Skynet optimize?

Skynet is Optimize Anything: every run optimizes a repository. Use a GitHub repository that you already have, or let Skynet create a new one from your task and data. The code can use any library.

Every engine works on repositories: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler and ShinkaEvolve.

**Auto** is the default. It explores with several engines, then continues GEPA from the best version. Read [How optimization works](/docs/concepts/optimization) and [Engines](/docs/concepts/engines).

### Which languages does the interface support?

The interface has 24 languages. Hebrew is the base language, and the layout supports right-to-left text. Hebrew docs are at [/docs/he](/docs/he).

### Is Skynet open source?

Yes. The code uses the AGPL-3.0 license. If you offer a changed version of Skynet as a service, the license requires you to share your changes.

## Data

### Which file types can I upload?

You can upload CSV, JSON, `.xlsx` and `.xls` files. For Excel files, Skynet reads only the first sheet. See [Prepare a dataset](/docs/guides/datasets).

### My data has no labels. What can I do?

Use the tagger. You can label rows yourself, or let an AI model label them while you review its work. See [Label data with the tagger](/docs/guides/tagger).

### Who can see my runs and datasets?

> [!WARNING]
> New runs are **Public** by default. A public run appears in public search, and anyone can browse its results.

Set **Visibility** to **Private** when you submit a run to keep it out of public search. New datasets are **Restricted**: only you and the people you invite can open them. See [Sharing](/docs/guides/sharing) and [Security](/docs/security).

### Do model providers train on my prompts?

By default, no. The default **Model data privacy** option is **No training**. It sends prompts only to providers that do not train on them and do not keep them. This setting applies to calls that Skynet bills to your balance. BYOK calls through OpenRouter follow your OpenRouter account's privacy settings. Native provider BYOK calls follow that provider's data policy. See [Security](/docs/security).

## Billing

### How does Skynet charge for runs?

Skynet uses a prepaid balance. Managed model usage and markup and sandbox usage are metered. A fee applies to balance top-ups. Before paid work starts, Skynet reserves headroom in your wallet and in the run budget. See [Billing](/docs/concepts/billing).

### Can I use my own model provider key?

Yes. With your own key (BYOK), your provider bills model tokens to your key. Skynet charges no model fee. The run budget covers sandbox usage only. Skynet encrypts your key and never shows it again. See [Billing](/docs/concepts/billing) and [Security](/docs/security).

### Can I run Skynet without paying for model calls?

The [Mac app](/docs/guides/mac-app) runs Skynet on your own computer. It sends model calls through Claude Code or Codex, signed in on your Mac. Your CLI plan pays for those calls, so they add $0 to the run.

## Tools and integrations

### Can I control Skynet from my own AI client?

Yes. Skynet has an MCP server at `https://mcp.skynetml.com/mcp`. You sign in with OAuth. The server has these tools: `check_run`, `start_run`, `get_run`, `list_runs` and `stop_run`. See [MCP](/docs/mcp).

### Is there an API?

Yes. Generate an API token in Settings, then send it as `Authorization: Bearer <token>`. Each user has one valid token at a time. See [API](/docs/api).

### Can the agent panel delete my runs?

Only if you allow it. In the default **Ask** mode, the agent asks before every action. In **Automatic** mode, it still asks before destructive actions, such as delete or submit. See [Agent panel](/docs/guides/agent-panel).
