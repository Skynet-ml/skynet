<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="frontend/public/brand/hoatzin-dark-256.webp" />
    <img src="frontend/public/brand/hoatzin-light-256.webp" width="128" alt="Skynet" />
  </picture>
</p>

<h1 align="center">Skynet</h1>

<p align="center"><b>Optimize Anything.</b> Give Skynet a prompt, code, a DSPy program or any text, a set of cases and a metric. Skynet makes it better and proves it.</p>

<p align="center">
  <a href="https://skynetml.com">skynetml.com</a> ·
  <a href="https://skynetml.com/docs">Docs</a> ·
  <a href="https://skynetml.com/docs/quickstart">Quickstart</a> ·
  <a href="https://skynetml.com/docs/api">API</a> ·
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL_v3-blue.svg" alt="License: AGPL v3" align="center" /></a>
</p>

## How it works

You write two functions: `run(artifact, input)` and `score(input, output, expected)`. Skynet proposes new versions, runs each one on your cases in a sandbox, scores them with your metric and keeps the best version. You see the held-out lift against your starting version.

## Features

- **One product, many engines.** GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler and ShinkaEvolve for prompts, code and text. DSPy GEPA, MIPROv2 and BootstrapFewShot for DSPy programs. **Auto** picks for you. [Engines](https://skynetml.com/docs/concepts/engines)
- **Safe execution.** Your code runs in an isolated sandbox. Each run has a spending limit, reserved before paid work starts. [Billing](https://skynetml.com/docs/concepts/billing) · [Security](https://skynetml.com/docs/security)
- **Use it from your agent.** The hosted MCP server (`https://mcp.skynetml.com/mcp`, OAuth 2.1) lets Claude, Codex and other MCP clients check, start, follow and stop runs. The [Skynet plugin](plugins/skynet) adds ready skills for Claude and Codex. [MCP](https://skynetml.com/docs/mcp)
- **Skynet for Mac.** Run the whole stack on your Mac. Model calls go through Claude Code or Codex, so your CLI plan pays for them. [Mac app](https://skynetml.com/docs/guides/mac-app) · [desktop/](desktop)
- **Agent panel.** An assistant that sets up runs, reads any file you attach and explains results. [Agent panel](https://skynetml.com/docs/guides/agent-panel)
- **Datasets and AI tagging.** Edit datasets in place. The tagger learns your labels, then tags the rest in reviewed rounds. [Datasets](https://skynetml.com/docs/guides/datasets) · [Tagger](https://skynetml.com/docs/guides/tagger)
- **Results you can use.** Compare versions, share a run and export the best artifact. [Results](https://skynetml.com/docs/guides/results)
- **Prepaid balance or your own key.** Bring your own OpenRouter key and pay only the platform fee and sandbox usage.
- **24 languages, RTL first.** Hebrew is the base locale. The docs are in 17 languages.

## Run it locally

You need Python 3.11, Node 20+, PostgreSQL 15+, Docker, [`just`](https://github.com/casey/just) and [`uv`](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/Skynet-ml/skynet.git && cd skynet
just setup      # installs deps, writes env files with fresh secrets, runs migrations
just dev        # API on :8000, web app on :3000
```

With Claude Code, `just install-agent` runs the setup and fixes known problems for you. Open http://localhost:3000. The API reference is at http://localhost:8000/scalar. Run `just --list` for all recipes.

## Repository

```
frontend/   Next.js web app and the public docs (/docs)
backend/    FastAPI API, job workers, engines, MCP server
desktop/    Skynet for Mac (Electron)
plugins/    Skynet plugin for Claude and Codex
i18n/       UI catalogs (Hebrew base + 23 locales)
infra/      Terraform for AWS staging and production
```

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and i18n rules. [MAINTENANCE.md](MAINTENANCE.md) says what support to expect. Report vulnerabilities through [SECURITY.md](SECURITY.md).

## Citations

If you use these methods through Skynet in research, cite the original work.

- Lakshya A Agrawal et al. *GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning.* 2025. [arXiv:2507.19457](https://arxiv.org/abs/2507.19457)
- Yoonho Lee et al. *Meta-Harness: End-to-End Optimization of Model Harnesses.* 2026. [arXiv:2603.28052](https://arxiv.org/abs/2603.28052)

## License

[AGPL-3.0](LICENSE). If you offer a modified Skynet as a service, share your changes.
