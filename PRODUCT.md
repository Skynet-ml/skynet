# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Two groups, in this order of priority for new power features:

- Engineers who point Skynet at their own code, prompts or repositories to push a score up, and who check on long runs every so often over hours or overnight.
- People with a dataset and a task, many of them not engineers, who use the guided wizard to optimize an LLM program.

Open decision: a control that lets each user choose how much abstraction Skynet shows, from fully guided to raw configuration. Build it after repo optimization ships, then use it to find out which group a surface should serve by default.

## Product Purpose

Skynet is a self-hostable platform that optimizes the code in a repository against a metric that the user owns, and serves the result. Users can:

- Turn a dataset and a task into a new GitHub repository with a first solution, then optimize it and see the held-out lift it earned.
- Use "Optimize Anything" to hill-climb an existing repository against a scorer they write. Every run optimizes a repository.

Success means the user ends up with a measurably better artifact they can trust and use, together with evidence of how much better it is.

## Positioning

- The user owns the metric. Skynet runs the search, the sandboxing, the budget and the evidence, and it verifies the user's scorer before spending anything on it.
- Runs are metered at provider cost plus a flat markup.

## Constraints

- RTL-first with 24 locales. Hebrew is the base language. New UI strings go into he.json and en.json, then get generated for the other locales.
- User code runs only inside the managed sandbox, which is offline apart from the trusted gateway.
- Git flow: changes reach users through pull requests, never direct pushes to a default branch.

## Terminology

- **Run**: one optimization job.
- **Version** or **candidate**: one proposed artifact.
- **Scorer**: the user's metric function, `(candidate, case) -> (score, side_info)`.
- **Setup check**: the preflight that verifies a run's inputs inside the sandbox before the run is paid for.
