# Repository constraints: `optimize.toml`

A repository run reads an `optimize.toml` file at the root of the repository,
at the pinned commit. The file can only narrow the settings that the run's
owner approved. It cannot widen them. Each secret that a service needs comes
from the run, never from the file.

```toml
# Required. The only version. Put it at the top, before any section.
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
# Optional. Extra hosts the proposer's box can reach, on top of the relay and
# the package registries. [] means the relay only.
network = ["docs.python.org"]

[data]
# Optional. A JSONL file of the run's cases. Omit [data] when the metric needs
# no cases.
cases = "data/cases.jsonl"

[outcome]
# Optional. The score that is good enough. The search stops when it reaches it.
target = 0.9

[outcome.require]
# Optional. A minimum for each named score that the metric returns.
latency = 0.5

[tool.my-team]
# Optional. Skynet defines no key here and does not read this table.
owner = "search team"
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

## Rules of the file format

- `version = 1` is required. A file without `version` fails the setup
  check, and the message shows the fix: put `version = 1` at the top of the
  file. An empty file fails too.
- A `[tool.<name>]` table is accepted and skipped. Skynet defines no key
  there and does not read its content. It is the one place for extra data,
  for example the settings of another tool. `tool` must be a table, and each
  `tool.<name>` must be a table.
- A key that the format does not define is an error in every section.

The worker never reads the metric. The metric lives in the repository, and
the worker gets a copy of the repository without the metric files. Or the
metric lives with the judge.

## Outcome: `[outcome]`

`[outcome]` says which score is good enough. It has two optional keys:

- `target`: the score that is good enough, for example `0.9`.
- `require`: written as an `[outcome.require]` table. Each key is a score
  name from the `scores` that the metric returns, and each value is the
  minimum for that score.

Each value must be a finite number. `true`, `false`, `nan` and `inf` fail.
An integer is read as a number, so `1` means `1.0`. Skynet sets no range,
because the scorer sets no range for a score. A score name must not be
empty. `[outcome.require]` holds at most 50 names. An empty `[outcome]`, or
an `[outcome]` with only `[outcome.require]`, is valid.

At the end of a run, Skynet judges `[outcome]` on the final score of the
reported version. Skynet splits the cases 70/15/15 with the run seed. The
engine can stop on its search score, but the verdict uses the held-out
score: the mean of `score()` over the held-out cases. With fewer than 5
cases there is no held-out part, so the run uses its validation cases, or
its train cases when there are none. A
run with no cases has no split, so the verdict uses the one `score()` of
the version. "Target reached" is true when that score is equal to or above
`target`. "Minimums passed" is true when each named score is equal to or
above its minimum. A named score is its mean over the same cases. The
metric must return the name on each of those cases, or the minimum fails.
The reason gives the count, or names the score when no case returns it.
The run stores the verdict as `details.outcome` in its result, with
the list of the failed minimums. The verdict does not change what the run
publishes.

`[outcome]` has no `accept` key, and an `accept` key fails as an unknown
setting. Every run opens a pull request, as before. Skynet does not merge it
by itself.

The search stops when its score reaches `target`. A file without `target`
has no stop score.

## Run record: `details.record`

A run with an `optimize.toml` stores a record that lets another person
reproduce its score. The record is `details.record` in the run result, and
`get_run` returns it. A pull request from the run shows the same record in
a short block. The first line of the block says if the run met the
contract, which rule failed with its numbers, or that the file sets no
target and no minimum. The record holds no secret, no case content and no
metric code. It has these keys:

- `contract_sha256`: the SHA-256 of the `optimize.toml` bytes the run read,
  after the draft files apply.
- `metric_sha256`: one SHA-256 over framed parts. The first part is the
  entry code, with the path `metric.py`. Then comes one part for each file
  under the `[metric] files` paths, in sorted path order. Each part is
  `<path in UTF-8>\0<byte length in decimal>\0<SHA-256 of the bytes>`. The
  SHA-256 is 64 lowercase hex characters, as `sha256sum` prints it. When
  the staged tree lists a path twice, the later entry counts, as extraction
  writes it last. For an entry code and one file, this gives the hash:
  `{ printf 'metric.py\0%s\0%s' "$code_len" "$code_hex"; printf 'eval/scorer.py\0%s\0%s' "$len" "$hex"; } | sha256sum`.
- `metric_home`: `repository` when the file sets `[metric] files` and every
  one comes from the pinned commit. `judge` when the file sets no
  `[metric] files`, or a draft file replaces or adds one.
- `cases_sha256` and `cases_source`: with `[data] cases`, the SHA-256 of the
  file bytes and `file`. With uploaded cases, the SHA-256 of the cases as
  compact JSON with sorted keys in UTF-8 (`json.dumps(cases,
  sort_keys=True, separators=(",", ":"), ensure_ascii=False)`) and
  `request`. Both are null when the run has no cases.
- `seed`: the run seed, or null when the run sets none.
- `split`: null when the run has no cases. Otherwise `rule` (`pairs`, or
  `single` for one case), `seed` (the split seed, 0 when the run sets none),
  and the case positions of `search`, `validation` and `held_out`. A
  position is an index in the order of the cases the run received. `single`
  puts the one case in every part.
- `image`: the sandbox image pinned for the run (`name@sha256:<hex>` on
  hosted runs, the Docker image id on local runs), or null.
- `target_reached`, `minimums_passed` and `failed_minimums`: copied from
  `details.outcome`. Each key is there only when `details.outcome` has it.
- `contract_met`: true when no rule failed. It is also true when the file
  sets no target and no minimum.

A run from before the record, or a run whose sandbox image writes no split
positions, has no `record` key.

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
- The file is hidden like the `[metric]` files (see "What is hidden"). The
  optimization agent and the candidate never see it. Only the parent's
  scorer reads it.
- The run shuffles the cases with its seed and splits them 70/15/15 into
  train, validation and test. The same file and seed give the same split.
  `[data] split` is not supported.
- A run that uploads its own cases for a repository with `[data]` fails:
  "This repo defines its cases in optimize.toml. Remove the uploaded cases."
- Without `[data]`, the run uses its uploaded cases, if any. With no cases at
  all, the candidate runs once with no input (`None`, or `null` on stdin),
  and the metric judges that one run.

## What is hidden

Only the parent's scorer reads the scoring side of the repository. Staging
takes these paths out of the optimization agent's tree, and the candidate's
box holds none of them while version code runs:

- `optimize.toml` itself.
- Every `[metric] files` path.
- The `[metric] exploits` folder (default `.optimize/exploits`).
- The `[metric] reference` file.
- The `[data] cases` file.
- The `[metric] illegal` file.
- The `[metric] mcp` file.

A key that the file does not set adds nothing to the list. A path that a
visible role also names stays visible. For example, one `.mcp.json` for
`[run] mcp` and `[metric] mcp` stays in the tree, because the candidate needs
it. The `instructions` file, the `[proposer] illegal` file and the
`[run] illegal` file always stay visible.

`optimize.toml` is always hidden, also when a visible setting names it. The
file is refused in these cases:

- A visible setting names a hidden path or a path inside one. The visible
  settings are the `instructions` file, an `illegal` file of `[proposer]`,
  `[run]` or a `[target.<name>]`, and the `mcp` file of one of those roles.
  The hidden paths here are `optimize.toml`, a `[metric] files` path, the
  exploits folder, the reference and the cases file.
- The `[run] entry` names a file that is in the hidden list or inside one
  of its paths. This includes `optimize.toml`, the `[metric] illegal` file
  and the `[metric] mcp` file.
- An editable path is in the hidden list or inside one of its paths. This
  also includes `optimize.toml`, the `[metric] illegal` file and the
  `[metric] mcp` file.

An `mcp` file that a visible role also names is not hidden, so the
`[run] entry` and the editable paths may name it. The agent's tree leaves
out everything inside a hidden path, and the brief names the entry and the
editable paths. So these refusals keep every hidden path out of the tree's
rule files and out of the brief.

The file is also refused when a hidden path is a symlink, lies inside a
symlinked folder, or holds a symlink or a hard link to a file outside it.
The strip matches names, so a link would leave its target in the agent's
tree.

The run stops before any model is called when the agent's tree still holds
a hidden file. A version's edit to a hidden path, `optimize.toml` included,
is dropped from the result.

The optimization agent never reads `optimize.toml`. It reads its brief,
which holds:

- The editable paths.
- The protected paths that are not hidden.
- The `[run] entry` and its timeout.
- The `[proposer] illegal` rules, numbered, and the `[run] illegal` rules.
- The `instructions` guidance.

The brief names no hidden path. It says only that some files are hidden and
that the agent cannot change them.

## Candidate box and metric box

Each box holds only the code that it needs. The run uses these boxes:

- **Setup box.** It runs once, before any version. It gets the repository
  without the metric files, runs `[setup] command` with package-registry
  network only, and closes. The parent saves the prepared state and its
  digest.
- **Metric setup.** It runs once, in a separate box, only when the
  repository has `.optimize/metric-requirements.txt`. It installs only the
  packages in that file, with package-registry network for the install only.
  The parent saves this second state. It holds no repository code.
- **Candidate box.** Each evaluation opens a new one from the prepared
  state. The parent checks the digest and lays out the version. The box
  runs the `[run]` entry on the case's `input` and reaches only the `[run]`
  services and MCP servers. The parent reads the output, then closes the
  box. Every process that the version started ends with the box.
- **Metric box.** Each evaluation opens a new one from the metric state,
  while the candidate box runs. It holds only the metric files and the
  scorer's runner, never the repository tree. It gets the metric files and
  the `[run]` output only after the candidate box closed. It reaches only
  the `[metric]` services and MCP servers.

Each box reserves its own budget hold, and releases or settles it when the
evaluation ends or fails. The relay refuses a `[metric]` call from the
candidate box and a `[run]` call from the metric box.

The metric is called as `score(repo_path, case, output)`. In the metric
box, `repo_path` is a folder that holds only the `[metric] files`, at their
repository paths. For example, `[metric] files = ["eval"]` puts the metric
at `repo_path/eval/`. The folder holds no other repository file and no
version code.

`.optimize/metric-requirements.txt` lists the metric's packages, one
requirement on each line, in the pip format (for example `numpy==2.1.0`).
Comments and blank lines are allowed. Options such as `-r`, `-e` or
`--index-url`, and paths or URLs, fail setup. The packages go into a folder
in the metric box, and the metric's runner puts that folder on `sys.path`
before the image's packages. Without the file, the metric box has only the
image's packages.

The metric cannot run version code. A check that needs the version's code
belongs in a `[run]` entry: the entry runs the code in the candidate box, and
the metric judges the entry's output. Setup warns when a metric file imports
a repository module that is not a metric file. The warning names the module
and the fix: move the check into a `[run]` entry. The metric box holds no
version file, so a `[metric]` needs a `[run]` section or a
`[target.<name>]` stage whose outputs it scores. A `[metric]` with neither
fails setup, because it would give each version the same score.

The box user can be root. The boxes depend on the microVM boundary between
boxes, not on file permissions inside a box.

## Run time for the metric

Skynet measures how long the `[run] entry` takes on each case. A metric
gets this time when it names an `elapsed_s` parameter after `output`:

```python
def score(repo_path, case, output=None, elapsed_s=None):
    ...
```

A keyword-only `*, elapsed_s=None` after `output` also works. The
parameter must come after `output`: `score(repo_path, case, *,
elapsed_s=None)` has no `output` parameter, so it does not get the time.
`elapsed_s` is
a float in seconds. When no `[run] entry` ran, the parameter keeps its
default. A metric with
`score(repo_path)`, `score(repo_path, case)` or
`score(repo_path, case, output)` gets the same call as before. A `*args` or
`**kwargs` metric does not get `elapsed_s`.

The `[run]` supervisor measures the time in its own process, with a
monotonic clock. The time starts just before the entry starts. It stops when
the entry exits and the supervisor kills the entry's process group. A
process that the entry starts in a new session is not part of that time,
and it ends when Skynet closes the candidate box. Until then, that process
can write any file that the entry can write. Thus the supervisor sends its
result as one line on its own stdout, not in a file. The entry never gets
that stdout: a function entry's prints go to stderr, and a command entry's
stdout is a pipe that the supervisor reads. The time includes the Python start
for a function entry. It does not include setup or the metric. The
entry's code runs in other processes, so it cannot patch this clock. A time
that the candidate puts in its `output` is not trusted. Use `elapsed_s`.

`elapsed_s` is only as trusted as the supervisor in the candidate box.
Each case gets a new candidate box, so candidate code from an earlier case
cannot change it. The metric runs in a separate metric box, so candidate
code cannot change the metric or the score.

The supervisor starts with the image's root-owned base interpreter
(`/usr/local/bin/python3`), not with `python3` from `PATH`. The box user can
write to `/app/.venv/bin`, so a `python3` there is not trusted. The
supervisor also starts without site-packages (`-I -S`), and Skynet writes the
runner again before each case. The supervisor starts a function entry with
`python3` from `PATH`, so the entry gets the packages that `[setup]`
installed. The entry is candidate code, so its interpreter does not need
trust; the clock stays in the supervisor. The shell that starts the
supervisor replaces itself with it (`exec`), so no shell of Skynet's waits
for its exit code. These steps make the supervisor harder to change. They
do not replace the separate boxes; see the residual risk below. Skynet
trusts the supervisor's result only when the supervisor exits cleanly, so an
entry that kills the supervisor gets no score. On Linux the supervisor
sets itself as not dumpable (`PR_SET_DUMPABLE`). This blocks `ptrace` and
`/proc/<pid>/mem` access to it, also when Yama is off. It also blocks access
to its open files through `/proc/<pid>/fd`.

On Vercel the supervisor runs as root (`sudo`). The box's exec wrapper sends
its stdout to a root-owned file in `/tmp` whose name holds a new random
token for each command. The supervisor checks that file before the entry
starts: root owns it, no other user can write it, it is not a link, and its
name matches the wrapper's. If a check fails, the supervisor stops and the
case fails. The
supervisor then starts the entry as the owner of the candidate tree, not as
root. So the entry cannot signal, trace or read the supervisor, and it
cannot replace or write the wrapper's output and exit code files (SKY-398).
Before the switch, the supervisor sets the Linux `no_new_privs` flag on
itself. The entry and every process it starts keep that flag, so the kernel
ignores the setuid bit of the box's `sudo`, and the entry cannot get back to
root. If the supervisor cannot set the flag, it does not start the entry, and
the case fails. The box image ships `sudo` only for the parent's own root
commands. The provider starts every other command under
`setpriv --no-new-privs`, so the same rule applies to them.
A root command never finds a program in a folder the box user can write,
such as `/app/.venv/bin`: the image's `sudo` rule and the provider both set
root's `PATH` to the system folders only. After the user switch, the
supervisor gives the entry the image's box `PATH` (`APP_ENTRY_PATH`), so the
entry still finds the `[setup]` packages.
A root supervisor that holds Linux capabilities always switches to the tree
owner, also when its stdout is a pipe. The entry runs as the supervisor's
user only when the supervisor is not root, or when it is a root with no
capabilities and a pipe stdout (a local Docker box drops all capabilities).
Such a root cannot switch users, and a pipe has no file that the entry could
replace.

The model mailbox wraps only the supervisor command in a root proxy. Other
root commands run without a proxy. The root proxy starts from
`/usr/local/bin/python3`, never from `PATH`. It runs only the proxy and
config bytes whose sha256 digests the parent sent, so a box user who
rewrites those files gets no code run as root.

Residual risk:

- Code that gets root in the box could still trace or change the supervisor
  process and its output files. `no_new_privs` blocks `sudo` and other setuid
  programs, but not a kernel exploit or a root command that runs a file the
  tree owner can change. Then only the separate boxes and the microVM
  boundary protect the metric and the score.
- The `no_new_privs` flag covers only the processes that Skynet starts.
  Vercel's own in-box agent and its file helpers run as the box user without
  the flag. So the block also depends on the box kernel refusing `ptrace`
  between unrelated processes of one user (Yama `ptrace_scope` 1 or higher).
  This is not verified yet.
- The `[run]` entry can plant a `.pth` file in the candidate box's
  site-packages. That file runs only in that candidate box. The box ends
  after the case, and the metric box never gets the file (SKY-398).
- Each case starts from the state that `[setup]` left. A cache that
  `[setup]` fills (for example a download or a model file) makes every case
  faster, but a case leaves nothing for the next case.
- Where the image has no `/usr/local/bin/python3` (for example a local test
  host), the supervisor falls back to `python3` from `PATH`.

A timeout or other `[run]` failure scores 0, and the metric does not run.

## What is enforced

| Setting | Enforcement |
| --- | --- |
| `[paths] editable` | The parent checks every version before it is scored or published. A path outside the approved editable paths fails the setup check. |
| `[paths] protected` | The parent rejects a version that changes a protected path, in its patch or while its code runs. A write that the code undoes before it exits also counts. The rejection names the path and the rule, with secret values hidden. `optimize.toml` itself is always protected and hidden. |
| `[proposer] network` | The proposer's box reaches the model relay and nothing else, except the hosts below. With no `network` key, it also reaches the package registries: the account's saved Python index (default `pypi.org` and `files.pythonhosted.org`) and `registry.npmjs.org`. A custom index replaces the PyPI hosts with the index's own host. `network = []` gives the relay only. `network = ["docs.python.org"]` gives the relay, the registries and that host. The run record shows the effective hosts under `roles`. |
| `[setup] command` | Runs once on the starting commit, in its own box, with package-registry network and no secrets. No version runs in that box. |
| `[run] network`, `[metric] network` | The candidate box or the metric box reaches only these hosts, besides the relay. With no `network` key, or `network = []`, the box is offline except for the relay. |
| `[run] vcpus`, `[metric] vcpus` | The size of the candidate box or the metric box: `1`, or an even number from 2 to 32. Another value fails the setup check. With no key, the box gets the run's default size. |
| `instructions` | Not enforced. The agent reads the file in its brief. The scorer decides the score. |
| `[proposer] illegal` | Enforced by judge models. See "Plain-language rules" below. |
| Rule files | The `instructions` file and every `illegal` file (in `[proposer]`, `[run]`, `[metric]` and `[target.<name>]`) are protected, so no version can change its own rules. |

## What fails the setup check

- A `services` list in a `[target.<name>]`, or a `network` host list in a
  `[target.<name>]`. Runs cannot use these settings yet. `network = []` in a
  stage passes, because a stage box is already offline except for the relay.
- A `tools` key in `[proposer]`, `[run]`, `[metric]` or `[target.<name>]`.
  The message says to list the MCP servers in a `.mcp.json` file and set
  `mcp = "<path>"` instead.
- A service or MCP server that breaks the secret rules below.
- An unknown section, or a setting that the format does not define in any
  section. A missing `version`, a `version` that is not `1`, or invalid TOML.
  A `tool` value or a `tool.<name>` value that is not a table.
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
command, the network for setup, for versions (`[run]`) and for the metric, and each role's services
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
  `services` list names. `[run]`, `[metric]` and `[proposer]` can have a
  `services` list now. In a `[target.<name>]`, it fails the setup check.
- The proposer reaches the services of `[proposer] services` through the
  parent's relay (SKY-612). Its box gets one `SKYNET_SERVICE_<NAME>_URL`
  for each service, with a loopback url and no secret. Skynet checks each
  call against the proposer's own list, fills the secrets in the parent,
  and returns redirects without following them.
- `[proposer]`, `[run]`, `[metric]` and each `[target.<name>]` can name a
  standard `.mcp.json` file with `mcp = "<path>"`. A role sees only the
  servers of its own file, so the proposer gets none of the metric's
  servers. Skynet reads the files from the same commit as `optimize.toml`,
  checks them with the same secret rules, lists their servers in the
  environment record, and protects them like rule files.
- A `[target.<name>]` stage reaches the servers of its own `mcp` file
  through its box's relay (SKY-611). The stage box's copy of the file lists
  only that stage's servers, each with a loopback url, and holds no secret.
  The stage also gets `SKYNET_MCP_<NAME>_URL` and `SKYNET_MCP_CONFIG`.
  Skynet checks each call against that stage's own file, so a stage cannot
  reach the proposer's, the scorer's or another stage's servers.
- A stage with an `mcp` file runs calls in parallel, up to the run's
  concurrency, like any other stage (SKY-624). Each call serves its own
  relay on its own loopback port. Each call gets its own copy of the MCP
  file with that port, and `SKYNET_MCP_CONFIG` names that copy. Read the
  servers from `SKYNET_MCP_CONFIG` or `SKYNET_MCP_<NAME>_URL`. The copy of
  the file in the stage's tree names a fixed port that no call serves.
- Only http servers are supported now. Each server has a `url` and goes
  through Skynet's relay. A stdio server (`command`, `args`, `env`) fails
  the setup check before any sandbox starts.
- Each `url` must be `https` on a public host. The check refuses local,
  private and link-local addresses.
- Secrets appear only as `${NAME}` in `headers`. A value that looks like a real key fails with a message that says
  to use a placeholder. The old `secrets = [...]` key fails with a message
  that says to move each name into `headers`.
- An API that reads its key from the URL query gets a `query` table. Each
  value must be exactly one `${NAME}`; any other value fails the setup
  check. For `[run]` and `[metric]` calls, the relay adds the filled
  parameters to the URL in the parent. It drops any parameter of the same
  name that the code sends, and it removes the value from the answer, as
  for headers. Only `[run]` and `[metric]` can list a service with a
  `query`; the setup check refuses it in `[proposer]` and `[target.<name>]`.

  ```toml
  [services.geocode]
  url = "https://maps.googleapis.com/maps/api/geocode"
  query = { key = "${GOOGLE_MAPS_KEY}" }
  ```
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
   this text and continues: "Illegal action. Read the repository rules in
   your brief. You cannot do this action. Do not repeat it, because it
   wastes time." This is
   the same for the parent's check and for the sandbox's early checks:
   AutoResearch `eval.sh`, Meta-Harness `save.sh` and its candidate check, the
   AutoSaddler verifier, and the ShinkaEvolve file-edit check (model and agent
   editors). A blocked version uses no scoring budget.
2. **Judge during the run.** About every 15 agent actions, a fast judge model
   reads the recent actions and the current diff against the rules. When it
   sees work that goes toward a violation, the agent's next scoring reply
   carries this note: "Check the rules in your brief. You are probably about
   to break one of them: <reason>." The agent may continue.
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
