# Repository constraints: `optimize.toml`

A repository run reads an `optimize.toml` file at the root of the repository,
at the pinned commit. The file can only narrow the settings that the run's
owner approved. It cannot widen them. Each secret that a service needs comes
from the run, never from the file.

```toml
version = 1
# Optional. A markdown file with the run's intent. The optimization agent reads
# it as guidance; it is not enforced. Put it before any section.
instructions = "docs/intent.md"

[paths]
# Optional. Each path must be inside an approved editable path.
editable = ["bot/geocode"]
# Optional. No version may change these, even inside an editable path.
protected = ["bot/geocode/eval", "data/labels.csv"]

[setup]
# Optional. Used only when the run has no setup command of its own.
command = "pip install -r requirements.txt"

[proposer]
# Optional. A markdown file of plain-language illegal approaches. Judge models
# enforce them.
illegal = "docs/rules.md"

[data]
# Optional. A JSONL file of the run's cases. Omit [data] when the metric needs
# no cases.
cases = "data/cases.jsonl"
```

`docs/rules.md` holds one rule in each top-level bullet:

```markdown
# Rules

- Do not hardcode answers for dataset cases.
- Do not call an outside geocoding service.
```

The rule text lives in these files, not in `optimize.toml`. The old
`[instructions]` and `[rules]` sections, and an `illegal` list written in
the file, fail the setup check with a message that says to move the text
into a file.

## Cases: `[data] cases`

`[data] cases` names the only source of a repository run's cases. The file
holds one case on each line, as a JSON object:

```json
{"input": "Paris, the capital", "expected": [48.85, 2.35]}
{"input": {"text": "Rome"}}
```

- `input` is required. It goes to the candidate: a function entry gets it as
  its argument, and a command entry gets it as JSON on stdin.
- `expected` is optional. Only the metric gets it, in the whole case.
- Blank lines are skipped. A UTF-8 byte-order mark at the start is allowed.
- The file is hidden like the `[metric]` files. The optimization agent and
  the candidate never see it. Only the parent's scorer reads it.
- The run shuffles the cases with its seed and splits them 70/15/15 into
  train, validation and test. The same file and seed give the same split.
  `[data] split` is not supported.
- A run that uploads its own cases for a repository with `[data]` fails:
  "This repo defines its cases in optimize.toml. Remove the uploaded cases."
- Without `[data]`, the run uses its uploaded cases, if any. With no cases at
  all, the candidate runs once with no input (`None`, or `null` on stdin),
  and the metric judges that one run.

## What is enforced

| Setting | Enforcement |
| --- | --- |
| `[paths] editable` | The parent checks every version before it is scored or published. A path outside the approved editable paths fails the setup check. |
| `[paths] protected` | The parent rejects a version that changes a protected path, in its patch or while its code runs. A write that the code undoes before it exits also counts. The rejection names the path and the rule, with secret values hidden. `optimize.toml` itself is always protected. |
| `[setup] command` | Runs once on the starting commit, with package-registry network and no secrets. The box goes offline before any version runs. |
| `instructions` | Not enforced. The agent reads the file in its brief. The scorer decides the score. |
| `[proposer] illegal` | Enforced by judge models. See "Plain-language rules" below. |
| Rule files | The `instructions` file and every `illegal` file (in `[proposer]`, `[run]`, `[metric]` and `[target.<name>]`) are protected like `optimize.toml`, so no version can change its own rules. |

## What fails the setup check

- A `[services]` section, a `services` list, or an `mcp` file in any role.
  The file format below is checked, but runs cannot use these settings yet
  (SKY-385).
- A `tools` key in `[proposer]`, `[run]`, `[metric]` or `[target.<name>]`.
  The message says to list the MCP servers in a `.mcp.json` file and set
  `mcp = "<path>"` instead.
- A service or MCP server that breaks the secret rules below.
- An unknown section or setting, a `version` other than `1`, or invalid TOML.
- A path that leaves the repository, names `.git`, or protects the whole tree.
- A path with `*`, `?` or `[`. Paths are literal; patterns are not supported,
  so name the files or folders.
- An empty `[paths] editable` list, or protected paths that cover every
  editable path. Such a run could not change anything. This includes a run
  without the file whose only approved editable path is `optimize.toml`,
  because `optimize.toml` is always protected.
- An `optimize.toml` that is a link or a folder, or a file larger than 64 KiB.
- An `instructions` or `illegal` path that is absolute, leaves the
  repository, or names no file. A rule file that is a link or a folder, is
  larger than 32 KiB, or is not UTF-8 text. An empty instructions file, or
  one longer than 4,000 characters.
- A rule file with no bullets, more than 50 rules, a rule longer than 500
  characters, or text outside a top-level bullet. A line that starts with
  `- `, `* ` or `+ ` at the start of the line opens a rule. Indented lines
  and lines right after it continue that rule. Headings (`#` to `######`,
  then a space, a tab or the line end) and blank lines separate rules. Any other line that starts
  with `#` is rule text.
- A `[data]` section without `cases`, or with `split` or any other key. A
  `cases` path that is absolute, leaves the repository, or names no file. A
  `cases` path that is also the `instructions` file or an `illegal` file,
  because the agent reads those files in its brief. A
  cases file that is a link or a folder, is larger than 20 MiB, is not UTF-8
  text, holds no case or more than 200,000 cases. A line that is not a JSON
  object with `input` and an optional `expected`; the message names the line.

## Where the effective environment is recorded

The setup check response includes `environment`. The run saves the same
record under `target.repo.environment`. It holds the commit, the editable,
protected and read-only paths, the secret names (never values), the setup
command, the network for setup and for versions, and each role's services
and MCP servers.
A resumed run that arrives at different rules fails instead of continuing.

## Services, MCP files and secrets

A service is an outside API that a role calls. Write it with a `url` and
`headers`, and write each secret only as a `${NAME}` placeholder:

```toml
[services.judge]
url = "https://api.example.com/v1"
headers = { Authorization = "Bearer ${JUDGE_KEY}" }

[metric]
services = ["judge"]
mcp = "eval/.mcp.json"
```

- There are no default services. A role gets only the services that its own
  `services` list names.
- `[proposer]`, `[run]`, `[metric]` and each `[target.<name>]` can name a
  standard `.mcp.json` file with `mcp = "<path>"`. A role sees only the
  servers of its own file, so the proposer gets none of the metric's
  servers. Skynet reads the files from the same commit as `optimize.toml`,
  checks them with the same secret rules, lists their servers in the
  environment record, and protects them like rule files. No box gets the
  proposer's or a target's servers yet: SKY-385 owns that wiring.
- Only http servers are supported now. Each server has a `url` and goes
  through Skynet's relay. A stdio server (`command`, `args`, `env`) fails
  the setup check before any sandbox starts.
- Each `url` must be `https` on a public host. The check refuses local,
  private and link-local addresses.
- Secrets appear only as `${NAME}` in `headers`. A value that looks like a real key fails with a message that says
  to use a placeholder. The old `secrets = [...]` key fails with a message
  that says to move each name into `headers`.
- Each secret goes to one host only: the host of the `url` that uses it. A
  file that sends the same `${NAME}` to two hosts fails.
- The run, not the repository, says where each user secret may go. Each
  secret in `target.repo.secrets` has `allowed_hosts`: up to 20 entries, each
  an exact host (`api.example.com`) or a suffix (`*.example.com`, which
  matches `eu.api.example.com` but not `example.com`). A `[services]` url and
  an MCP server url must both use a host on the list. An empty or missing
  list allows no host, so a shared file cannot send your key to a host you
  did not choose.
- Each `${NAME}` binds to `user` (the user's own secret, with no Skynet fee)
  or `managed` (Skynet's key, metered for each call). The run sets this in
  `target.repo.secret_bindings`; a name it does not list is `user`. Managed
  works only for a host in Skynet's connector catalog, for example
  `https://mapstools.googleapis.com/mcp`, and only when Skynet holds a key
  for that host.
- The setup check fails before any sandbox starts when a user-bound secret
  is missing from the run, when its host is not on its `allowed_hosts`, or
  when a managed secret has no Skynet key. A managed secret goes only to its
  catalog host and needs no `allowed_hosts`.
- The relay removes each filled secret value from the answers it returns.
- Saved user secrets are encrypted at rest (Fernet) and are decrypted only
  in the trusted parent at run start.

## Plain-language rules

The `[proposer] illegal` file holds up to 50 rules, each up to 500 characters. The
agent's brief lists every rule at the start as "Rule 1", "Rule 2" and so on.
Skynet enforces them in three places:

1. **Blocked versions.** When the path rules refuse a version, the agent gets
   this text and continues: "Illegal action. Read optimize.toml and the rule
   files it names. You cannot do this action. Do not repeat it, because it
   wastes time." This is
   the same for the parent's check and for the sandbox's early checks:
   AutoResearch `eval.sh`, Meta-Harness `save.sh` and its candidate check, the
   AutoSaddler verifier, and the ShinkaEvolve file-edit check (model and agent
   editors). A blocked version uses no scoring budget.
2. **Judge during the run.** About every 15 agent actions, a fast judge model
   reads the recent actions and the current diff against the rules. When it
   sees work that goes toward a violation, the agent's next scoring reply
   carries this note: "Check the rule file that optimize.toml names. You are
   probably about to break one of its rules: <reason>." The agent may
   continue.
3. **Judge on each version.** Before the scorer runs, a stronger judge reads
   the full diff of the version. When the diff breaks a rule, the version is
   an "illegal approach": it gets a score of 0 and the judge's reason as its
   feedback, and the scorer does not run. The version stays in the run
   history with its score of 0, so later versions can learn from it. This is
   the same for every optimizer.

Each block, note and rejection is a run-log event (`rule.block`,
`rule.note`, `rule.rejection`) that names the rule and the reason. The run's
Logs tab shows them. A block from the parent has the source `host`. A block
from a sandbox check has the source `engine`. The judges read only the rules, the agent's actions and
the diff. They never read the scorer's data or the test labels. Their model
calls go through the run's metered model gateway on the managed key, so the
run budget pays for them. Only the parent can call the judge routes.

The judge models default to the model catalog's best-value model (fast
judge) and strongest model (final judge). Operators can set
`RULE_WATCH_MODEL` and `RULE_JUDGE_MODEL` to override them. When a file has
rules but the server has no managed model key, the setup check fails. When
the final judge cannot give a clear answer, or a diff is longer than 400,000
characters, the version gets a scoring error. It is not marked illegal.

Limits:

- A Meta-Harness `save.sh` block reaches the run log when the agent's
  session turn ends, not at the moment of the block.
- A ShinkaEvolve agent-editor block also arrives when the agent's session
  ends. The whole version is refused, and the block text is the version's
  error, which ShinkaEvolve keeps as the failed attempt's message.
- Without a constraints file, the ShinkaEvolve agent editor still drops
  changes outside the editable paths without a message, as before.
- Without a constraints file, the sandbox checks keep their earlier messages,
  which name the refused paths, and they write no `rule.block` event.
- The fast judge counts each answered model call of the agent as one action.
  Its note arrives with the next scoring reply, not in the middle of a turn.
- A version with no changes (the starting point) is not judged.
