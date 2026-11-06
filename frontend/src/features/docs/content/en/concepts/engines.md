An engine is the search method that proposes new versions of your artifact. Skynet has six native engines for repositories, three DSPy optimizers for DSPy programs, and **Auto**. This page says what each one does, when to pick it, and which settings it has.

Read [How optimization works](/docs/concepts/optimization) first if you do not know which engines fit your artifact.

## Quick comparison

| Engine                | Artifact kinds     | Coding agent settings              | Max iterations setting |
| --------------------- | ------------------ | ---------------------------------- | ---------------------- |
| Auto | DSPy program | No | No |
| GEPA | Repository | No | No |
| Best-of-N | Repository | No | No |
| AutoResearch | Repository | Yes | No |
| Meta-Harness | Repository | Yes | Yes |
| AutoSaddler | Repository | Yes | Yes |
| ShinkaEvolve | Repository | No. It has its own editor setting. | Yes |
| DSPy GEPA             | DSPy program       | No                                 | No                     |
| DSPy MIPROv2          | DSPy program       | No                                 | No                     |
| DSPy BootstrapFewShot | DSPy program       | No                                 | No                     |

The wizard marks GEPA, DSPy GEPA and Auto as optimizers that use the feedback text from your metric.

## Auto

Auto is the default. What it runs depends on the artifact type:

- **DSPy program**: Auto runs DSPy GEPA.
- **Repository**: Auto is not available. Select one native engine.

**Pick it when** you do not know which engine fits. This is the best place to start.

## GEPA

"Reflective evolution with a Pareto front of versions." The reflection model reads the score and the feedback of each case and writes a better version. GEPA keeps a front of versions that are each best on some cases.

**Pick it when** your metric gives specific feedback, and you want a strong single engine.

## Best-of-N

"Independent proposals from the reflection model; keep the best." Each proposal is independent of the others. Skynet re-scores the top proposals on the validation cases.

**Pick it when** you want a simple baseline to compare with other engines.

## AutoResearch

A coding agent runs research rounds. It edits, scores and keeps what helps until the budget runs out.

**Pick it when** a good change needs several steps of investigation.

## Meta-Harness

A coding agent proposes a batch of candidates each iteration. It uses the full history of candidates and scores.

**Pick it when** you want an agent that learns from the history of all earlier attempts. Use **Candidates per iteration** to set the batch size.

## AutoSaddler

A coding agent reads the feedback from your metric and patches the version. Skynet keeps only patches that a new scorer run confirms.

**Pick it when** your metric feedback points to clear, local defects.

## ShinkaEvolve

"Evolves many versions in parallel islands and learns which models improve them most." It is an evolutionary search over a population of candidates, with mutations and crossovers that a model writes.

**Pick it when** you want broad exploration and you have a larger budget.

## DSPy optimizers

| Optimizer             | What it does                                                                                                              |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| DSPy GEPA             | Reads the metric feedback and evolves the instructions of each predictor.                                                 |
| DSPy MIPROv2          | Proposes instructions and few-shot demos together, and searches their combinations with Bayesian optimization.            |
| DSPy BootstrapFewShot | Collects worked examples that the program gets right and adds them as few-shot demos. It does not use a reflection model. |

DSPy MIPROv2 and DSPy BootstrapFewShot need pairs (cases with expected outputs).

## Settings

### Budget

| Setting         | Default | Range                                                       |
| --------------- | ------- | ----------------------------------------------------------- |
| Max scorer runs | 200     | 1 to 100000                              |
| Stop at score   | Off     | 0 or more                                                   |
| Max iterations  | Off     | 1 to 1000. Only Meta-Harness, AutoSaddler and ShinkaEvolve. |

One scorer run is one call of your metric on one case. The final re-score of the starting version and the winner does not count against **Max scorer runs**.

### Coding agent settings

These settings apply to AutoResearch, Meta-Harness and AutoSaddler.

| Setting                      | Default | Range                                            |
| ---------------------------- | ------- | ------------------------------------------------ |
| Coding agent                 | Codex   | Pi, Codex, OpenCode, Prime Agent, Custom command |
| Candidates per iteration     | 3       | 1 to 8. |
| Max tool calls per candidate | 100     | 1 to 1000                                        |

### ShinkaEvolve settings

| Setting                                 | Default                                                     |
| --------------------------------------- | ----------------------------------------------------------- |
| How new versions are written            | Single model call. The other option is "Coding agent (Pi)". |
| Islands                                 | 2 (1 to 8)                                                  |
| Migration interval                      | 10                                                          |
| Parent selection                        | Weighted. Other options: Power law, Beam search.            |
| Diff edits / Full rewrites / Crossovers | 60% / 30% / 10%. They must add up to 100%.                  |
| Archive size                            | 40                                                          |
| Parallel evaluations                    | 2                                                           |
| Parallel proposals                      | 2                                                           |

## Related pages

- [How optimization works](/docs/concepts/optimization)
- [Metrics and scorers](/docs/concepts/scorers)
- [Runs](/docs/concepts/runs)
- [Billing](/docs/concepts/billing)
