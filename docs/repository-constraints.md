# Repository constraints: `ascent.toml`

A repository run reads an `ascent.toml` file at the root of the repository,
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
# Optional. Your own proposer command. Used only when the run names no
# proposer of its own. See "Your own proposer" below.
entry = ["node", "agent/my-agent.js"]
# Optional. Runs once in the proposer box, before the first session, for
# example to install the command.
setup = ["npm", "ci"]

[run]
# Required, unless a [target.<name>] stage runs the version. The metric scores
# the output of this entry.
entry = "bot/run.py:main"
# Optional. The image of the candidate box, pinned by its digest. Set image or
# dockerfile, not both. See "Box images: image and dockerfile" below.
# image = "python@sha256:<64 hex characters>"

[metric]
# Optional. A Dockerfile that builds the image of the metric box. Its folder is
# the build context. See "Box images: image and dockerfile" below.
# dockerfile = "eval/box/Dockerfile"
# Optional. The files of the metric. No version sees or changes them.
files = ["eval"]
# Optional. The metric itself. The default is "metric.py:score", the Python
# metric code of the run. A command list runs a metric in any language. See
# "The metric: [metric] entry" below.
entry = ["bash", "eval/metric.sh"]
# Optional. "maximize" (the default) or "minimize". With "minimize", a lower
# score is better.
direction = "maximize"
# Optional. One shell command that builds the metric one time, before the
# first metric call. See "Build the metric: [metric] setup" below.
# setup = "cd eval && cargo build --release"

[metric.scores]
# Optional. The direction of each named score that the metric returns:
# "maximize" (the default) or "minimize". See "Named score directions" below.
cost_usd = "minimize"

[data]
# Optional. A JSONL file of the run's cases. Omit [data] when the metric needs
# no cases.
cases = "data/cases.jsonl"

[outcome]
# Optional. The score that is good enough. The search stops when it reaches it.
target = 0.9

[outcome.require]
# Optional. A limit for each named score that the metric returns: a minimum
# for a maximize score, a maximum for a minimize score.
recall = 0.8
cost_usd = 0.05

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

The rule text lives in these files, not in `ascent.toml`. The old
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
- Each value has one strict form. Skynet does not fix a value for you: it
  does not strip spaces or change the case of a value. For example, an MCP
  server `type` must be `http`, `streamable-http` or `sse` in lowercase,
  and `[metric] setup` must not start or end with a space.

The worker never reads the metric. The metric lives in the repository, and
the worker gets a copy of the repository without the metric files. Or the
metric lives with the judge.

### Check the file outside Skynet

Skynet reads `ascent.toml` with the parser of the
[`openascent`](https://pypi.org/project/openascent/) package (SKY-760). The
same parser is in the package's `ascent` command, so a file that
`ascent check` accepts passes the same format checks in Skynet:

```bash
pip install openascent
ascent check ascent.toml
```

`ascent check` checks the file itself, not the files that it names, and it
reports all the errors in one pass. Skynet also reads the named files and
applies its own limits: a box has 1 vCPU or an even number from 2 to 32
vCPUs, and Skynet sets no maximum `timeout`. A file with many errors can
show a different first error in Skynet than before SKY-760, because the
parser finds all the errors and reports them in a fixed order.

`ascent schema` prints the JSON Schema of the file. The same schema is
`ascent.schema.json` at the root of the openascent repository. To check
the file in your editor, put this line at the top of the file:

```toml
#:schema https://raw.githubusercontent.com/Skynet-ml/openascent/main/ascent.schema.json
```

## Your own proposer: `[proposer] entry` and `setup`

`entry` names the command that proposes each new version. `setup` names a
command that prepares the repository for it. Write each one as a list of strings, for
example `["node", "agent/my-agent.js"]`. A plain string, an empty list, an
empty part or a part with a control character fails the setup check, and the
message names the key. Control characters include the C1 controls and the
bidirectional text controls (U+061C, U+200E, U+200F, U+202A to U+202E and
U+2066 to U+2069), because they can hide what a command does.

- The run's own proposer wins. When the run request names a proposer, the
  run ignores `entry` and `setup`, and the run record shows the note "The
  run's own proposer replaces the one in the contract file."
  A setting of that proposer that the engine does not apply fails the setup
  check (see "What fails the setup check").
- `setup` without `entry` fails the setup check.
- Only a single AutoResearch or Meta-Harness run can use `entry` today.
  Every other run with `entry` fails the setup check, and the message names
  `[proposer] entry`. This includes a single GEPA, Best-of-N, AutoSaddler or
  ShinkaEvolve run, and every Auto run. GEPA, Best-of-N and AutoSaddler ran
  the command in a native box, and every run now uses the worker's engine
  loop (SKY-728). SKY-828 brings the command back to those engines. The
  notes below about GEPA, Best-of-N and AutoSaddler apply from then.
- `setup` runs once in each proposer box, in the repository checkout where
  the command runs, before the first proposer session, also in a protected
  run. The run keeps a copy of the files that `setup` adds in the git
  folder, also the files that `.gitignore` names (such as `node_modules/`).
  GEPA and Best-of-N reset the checkout before and after each version, and
  AutoSaddler gives each session a new worktree. Each later session gets the
  kept copy back, and `setup` does not run again. A file of the version at
  the same path stays, and the copy does not replace it. The copy keeps
  symbolic links as links. A file that holds the absolute path of the first
  checkout (for example a Python virtual environment) can fail in an
  AutoSaddler worktree, because each worktree has a different path. The copy
  uses disk space in the box, and each later session spends time to copy it.
  The files it adds never join a version. In GEPA, Best-of-N and AutoSaddler,
  it has 30 minutes, but never more than the time that the proposer session
  has left: the setup time counts inside the session's time limit. If it fails
  or runs out of time, the run stops, and the message names the command and
  its output. When `setup` ends, the run stops every process that it left
  running. When the run stops a proposer session, it also stops a `setup` that
  runs.
- `setup` can write only files that git does not track. If it changes,
  adds to the index or removes a tracked file, the run stops. The message
  names the command and the changed files, because the change would join
  every version. Edits that the version made before `setup` ran do not count.
  The same applies to the new files of the version that git does not track
  yet (GEPA and Best-of-N): if `setup` changes or removes one, the run stops.
  If `setup` makes a commit or checks out another commit, the run stops too.
- In GEPA, Best-of-N and AutoSaddler, a file or folder name that `setup`
  adds must be UTF-8. It cannot hold a control character (such as a new
  line) or end in a space. The run cannot keep such a name out of the
  versions, so the run stops, and the message names the file. AutoResearch
  and Meta-Harness keep any UTF-8 name out of the versions. A name that is
  not UTF-8 stops the run there, because the run cannot store it.
- The command runs in the proposer's box, under the `[proposer]` rules. Its
  working directory is the repository checkout. It gets the absolute task file path in
  `ASCENT_TASK`, the model in `ASCENT_MODEL`, the model relay in
  `ASCENT_MODEL_URL` and the relay key in `ASCENT_MODEL_KEY`. The arguments run exactly as written: `{model}` and the
  other custom-harness placeholders stay plain text. In GEPA, Best-of-N and
  AutoSaddler, the files it changes are the new version.
- ShinkaEvolve refuses `entry` today. The command may reach a model only on
  your own model key, never on Skynet's key, and its box has no such model
  route until a metered relay lands. Its command support is on the worker's
  engine loop, but the engine does not turn it on yet. In that support, `setup`
  runs once, in its own box. Only the files that it writes into the
  repository carry over to each command box: the box is disposable, and the
  tree is the truth. If `setup` changes or removes a file of the repository,
  the run stops, and the message names the files.
- The command runs only in a repository checkout. AutoSaddler also starts
  reflect sessions in a plain folder, which edit no repository. The default
  proposer agent answers those sessions, without `setup`, and the box
  installs that agent beside your command.
- The run record names the command in a note: "The run uses the proposer
  command in the contract file: ...". The run page shows the command as the
  proposer.
- AutoResearch and Meta-Harness run the command on the worker's engine
  loop, in a single run. The engine runs the command once for
  each new version, over the parent version's tree. The engine keeps its own
  search and lab state. `ASCENT_TASK` names `.autoresearch/ascent-task.md`
  or `.meta-harness/ascent-task.md`. The file holds the goal, the past
  versions, their scores and the lab notes. The tree that the command
  leaves, without the lab folder and without the files that `setup` added,
  is the new version, and the engine scores it. `setup` runs once for each
  engine, over the first version, before the first command. If `setup`
  changes or removes a file of the repository, the run stops, and the
  message names the command and the changed files. If the run cannot remove
  the files that `setup` added (for example, a read-only folder), the run
  stops too. `setup` and each `entry` command have 30 minutes each. A
  `setup` that runs out of time stops the run. An `entry` command that runs
  out of time gives no version. A command that fails gives no version: it is
  a failed attempt, not a failed run. AutoResearch then ends the run,
  because a round with no version ends it. Meta-Harness goes on to its next
  iteration. In these two engines, the command gets `ASCENT_TASK` and
  `ASCENT_MODEL`. In a protected run, `setup` and the command also get the
  model relay in `ASCENT_MODEL_URL` and the run's scoped key in
  `ASCENT_MODEL_KEY`. Their output shows `[redacted]` in place of the key.
  Skynet redacts only values of 8 or more characters, so a short value
  cannot change other output. A run that is not protected gets no model
  relay and no key. The engine loop does not give the command `[proposer]
  services` or a reasoning effort. So a single run of these engines with
  `entry` and one of these settings fails the setup check. The message
  names the setting.
- ShinkaEvolve cannot use the command, because it writes versions with its
  own editor. A single ShinkaEvolve run with `entry` fails the setup check,
  and the message names `[proposer] entry`.
- Every repository file or folder that `entry` or `setup` names by its
  exact path, such as `agent/my-agent.js`, `scripts/setup.sh` or `agent` in
  `["npm", "ci", "--prefix", "agent"]`, is protected, like `ascent.toml`.
  The run reads each argument as a path relative to the checkout root, after
  it removes `./` and a final `/`. An absolute path, the root itself (`.`)
  and a path outside the checkout are never protected. A protected folder
  protects every file in it. Other files that the command uses are not
  protected: modules that it imports, a module in `python -m agent.main`, a
  script in `sh -c "node agent.js"`, or `package.json`. A version can change those
  files, and so change the proposer of the next versions. To prevent this,
  put your proposer's folder in `[paths] protected`, for example
  `protected = ["agent"]`.
- An `entry` or `setup` argument that names a symbolic link, a path in a
  linked folder, or a folder that holds a symbolic link at any depth, fails
  the setup check. Protection matches paths, so a version could change the
  file that the link points at. Name the real path, and keep links out of a
  named folder.
- `entry` and `setup` cannot contain `__ASCENT_MODEL_KEY__` or an older key
  token. The command reads the relay key from `ASCENT_MODEL_KEY`.
- Limits in this version: `[proposer] mcp` with `entry` fails the setup
  check, and the run's tool-call limit does not apply to your command.

## Direction: `[metric] direction`

`direction` says which way the score improves. It is `"maximize"` (the
default: a higher score is better) or `"minimize"` (a lower score is
better, for example a latency or an error count). Another value fails the
setup check, and the message names both values. The key is protected like
the rest of `ascent.toml`.

With `"minimize"`, the engines get the negated score, so each engine still
climbs. Every score that a person or an API client sees stays raw: the run
result, the versions, the progress events, the record and the pull request.
The best version is the one with the lowest score, and the improvement is
positive when the score went down. The run result has a `direction` field.

`[outcome.require]` does not follow `direction`. Each named score has its
own direction in `[metric.scores]`.

## Named score directions: `[metric.scores]`

`[metric.scores]` gives a direction to each named score in the `scores`
that the metric returns. Each key is a score name, and each value is
`"maximize"` or `"minimize"`. Another value fails the setup check, and the
message names both values. A score name must not be empty. The table holds
at most 50 names.

A named score that the table does not name is `"maximize"`. This is also
true when `[metric] direction = "minimize"`: the main direction does not
change the named scores.

```toml
[metric]
direction = "maximize"

[metric.scores]
cost_usd = "minimize"

[outcome.require]
recall = 0.8     # a minimum: recall must be 0.8 or more
cost_usd = 0.05  # a maximum: cost_usd must be 0.05 or less
```

The metric returns each named score as it is. Do not negate a cost or a
latency. For a minimize named score, the engines get the negated value, so
each engine still climbs. The proposer text says "(lower is better)" for
that score. Every named score that a person or an API client sees stays
raw. Each entry in `failed_requirements` holds the `direction` of its
score.

A run that sets an `[outcome.require]` limit or uses a minimize named
score needs a sandbox image at protocol 21 or later. With an older image,
the run fails before it starts, and the message tells you to rebuild and
re-pin the sandbox image.

## Outcome: `[outcome]`

`[outcome]` says which score is good enough. It has two optional keys:

- `target`: the score that is good enough, for example `0.9`.
- `require`: written as an `[outcome.require]` table. Each key is a score
  name from the `scores` that the metric returns, and each value is the
  limit for that score. The limit is a minimum for a maximize score and a
  maximum for a minimize score (see `[metric.scores]`).

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
`target`, or equal to or below it with `direction = "minimize"`.
"Requirements passed" is true when each named score obeys its limit: equal
to or above its minimum for a maximize score, or equal to or below its
maximum for a minimize score. A named score is its mean over the same
cases. The metric must return the name on each of those cases, or the limit
fails. The reason gives the count, or names the score when no case returns
it. The run stores the verdict as `details.outcome` in its result, with
the list of the failed requirements. Each failed entry has the score
`name`, its `direction`, its `limit`, the raw `value` (null when the metric
did not return it) and a `reason`. The verdict does not change what the run
publishes.

```json
{
  "target_reached": true,
  "requirements_passed": false,
  "failed_requirements": [
    {
      "name": "cost_usd",
      "direction": "minimize",
      "limit": 0.05,
      "value": 0.06,
      "reason": "'cost_usd' scored 0.06, above its maximum 0.05."
    }
  ]
}
```

`[outcome]` has no `accept` key, and an `accept` key fails as an unknown
setting. A run that improves the score opens a pull request, as before.
Skynet does not merge it by itself. A repository that Skynet created gets
no pull request: its top versions go to branches, and `skynet/best` holds
the best one.

The search stops when its score reaches `target` (at or below it with
`direction = "minimize"`). A file without `target` has no stop score.

## Run record: `details.record`

A run with an `ascent.toml` stores a record that lets another person
check its score. The record is `details.record` in the run result, and
`get_run` returns it. A pull request from the run shows the same record in
a short block. The first line of the block says if the run met the
contract, which rule failed with its numbers, or that the file sets no
target and no limit. Then comes one line for each fact. Last comes a
collapsed section, "Record as JSON". It holds the full record as one JSON
block, which parses to the same object as `details.record`. GitHub limits
the length of a pull request text. When the JSON is longer than 20,000
characters, for example in a run with thousands of cases, the pull request
shows no JSON block. One line then says that `get_run` returns the record.
The run writes no record file into the repository. The record holds no secret, no case
content and no metric code. It has these keys:

- `record_version`: the integer `1`, or `2` when `[run]` or `[metric]`
  sets `image` or `dockerfile`.
- `base_commit`: the full commit SHA that the patch applies to. Staging
  pins this commit.
- `change_sha256`: the SHA-256 of the reported version's patch text, as
  UTF-8 bytes. The reported version is `best_candidate` in the `get_run`
  result. The patch never holds an edit to a hidden path. It is null when
  the patch is empty, because there is no change to hash. A run that starts
  from a patch (`seed_candidate`, for example a continuous check) keeps a
  hash, also when no version scores better. To compute it by hand, write
  `best_candidate` with no added newline and hash the bytes:
  `jq -j '.result.best_candidate' run.json | sha256sum`. The
  patch applies to `base_commit` after the run's draft files apply. The
  pull request branch also holds those draft files, and can hold a test
  suite from hardening. So its diff can differ from `best_candidate`.
- `baseline_score`: the score of the start version (`baseline_score` of the
  run result), at full precision. It is null when the run has no such score.
- `final_score`: the score that the `[outcome]` verdict judges
  (`best_score` of the run result), at full precision. It is null when the
  run has no such score. The run scores the final version one time.
- `contract_sha256`: the SHA-256 of the `ascent.toml` bytes the run read,
  after the draft files apply.
- `metric_sha256`: one SHA-256 over framed parts, one part for each file
  that the metric box gets from `[metric] files`, in sorted path order. The
  rule is the same for a Python and a command `[metric] entry`. For a Python
  entry, the run's metric code counts as one file at the entry's path (for
  `"metric.py:score"`, the path `metric.py`). It replaces a repository file
  at that path. The entry value itself is not a part, because
  `contract_sha256` covers it. Each part is
  `<path in UTF-8>\0<byte length in decimal>\0<SHA-256 of the bytes>`. The
  SHA-256 is 64 lowercase hex characters, as `sha256sum` prints it. When
  the staged tree lists a path twice, the later entry counts, as extraction
  writes it last. For the two files `eval/metric.sh` and `eval/words.txt`,
  this gives the hash:
  `for f in eval/metric.sh eval/words.txt; do printf '%s\0%s\0%s' "$f" "$(wc -c < "$f" | tr -d ' ')" "$(sha256sum "$f" | cut -d' ' -f1)"; done | sha256sum`.
  List the files in byte order (`LC_ALL=C sort`). With no part, the hash is
  the SHA-256 of no bytes. With `[metric] setup`, the command is one more
  part. Its path is `/[metric] setup`, and its bytes are the command text in
  UTF-8, with all the whitespace at both ends removed, as Python
  `str.strip()` removes it. No file path starts with `/`, so the part cannot
  clash with a file. Sort this part with the file paths in byte order, like
  any other part. With a `[metric]` user image, its digest is one more part.
  Its path is `/[metric] image`, and its bytes are the digest text in UTF-8,
  for example `sha256:<hex>`. So the hash also pins the box that runs the
  metric.
- `metric_setup`: the `[metric] setup` command. The record has this key only
  when the file sets `[metric] setup`. The pull request block shows it as
  "Metric setup".
- `metric_source`: `repository` or `request`. `repository` when the file
  sets `[metric] files` and every one comes from the pinned commit.
  `request` when the file sets no `[metric] files`, or a draft file in the
  run request replaces or adds one.
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
- `boxes`: only in a version 2 record. One object for each role (`run`,
  `metric`) that sets a user image. It holds `digest`, `source` (`image` or
  `dockerfile`), `input_hash` (the build's input hash, or null for `image`)
  and `build_log` (`image-build:<id>`, or null for `image`). The pull request
  block shows one line for each box.
- `target_reached`, `requirements_passed` and `failed_requirements`: copied from
  `details.outcome`. Each key is there only when `details.outcome` has it.
- `contract_met`: true when no rule failed. It is also true when the file
  sets no target and no limit.

A run from before the record, or a run whose sandbox image writes no split
positions, has no `record` key.

A hosted run's default sandbox image is in a private registry, so an
outside person cannot pull it. Set `[metric] image` to a public image to let
another person score with the same metric box. The openascent record schema
describes version 1 only, so a version 2 record does not validate against it
yet.

## Command entries: one call rule

A `[run] entry` or a `[target.<name>] entry` is a Python function
(`"path/to/file.py:function"`) or a command list in any language. A command
follows one rule, the same for the candidate and for each stage (SKY-757):

- It reads the JSON file at the path in `ASCENT_INPUT`. The file holds one
  object, `{"input": <the case input>}`, or `{"input": null}` when the run
  has no cases.
- It writes `{"output": <any JSON>}` to the path in `ASCENT_OUTPUT`. The
  `output` value is what the metric scores.
- What it prints, on stdout or stderr, goes to the logs only. Nothing goes
  to the logs while the command runs. After the call ends, the logs get the
  last 64 KiB of its stdout, then a line break when stdout does not end with
  one, then the last 64 KiB of its stderr. A log of 64 KiB or more can start
  inside a secret, so Skynet drops its first line before it logs it. A log
  with no line break is not logged. Its stdin is empty.

A nonzero exit code, a missing output file, an output that is not valid JSON
or has another form, a time limit or an output file larger than the limit
fails the call. For a `[run] entry`, the case then gets no score, and the
metric does not run (SKY-751). For a stage, the metric's `openascent.call`
gets the failure as an error. For a command entry, the error shows at most the first 300
characters of the cause (a longer cause ends with "…"), then the last 800
characters of the command's stderr and of its stdout, with the run's secrets
masked. The box sends at most the last 4,000 characters of each log. That cut
can split a secret, so Skynet drops the start of a log that the box cut. The
drop is one character shorter than the longest secret, and it never stops
inside a whole secret. Skynet then masks the secrets, and only then cuts the
cause and each log. Thus no part of a masked secret (6 characters or more)
shows across a cut. When the output file is not valid JSON, the cause is
always "The output file is not valid JSON. Write one strict JSON value to
ASCENT_OUTPUT: UTF-8, no NaN or Infinity, no duplicate keys, no lone surrogate
escape." This cause quotes no part of the file. The feedback of a failed
`[run]` case keeps its last 1,988 characters after "Run failed: ". The cause
and the two tails always fit in that limit, so the feedback always starts with
the cause. The 300-character cut is for command entries only. A function
entry's feedback is its traceback, with the secrets masked, cut to the last
1,988 characters, so it ends with the exception. The error of a failed stage keeps only its first 500 characters.
Thus it can show less than the full tails: it shows the cause, then the start
of the stderr part, or of the stdout part when stderr is empty. Each call gets
a new output path, so a file
from an earlier call is never read again. A function entry is not changed:
it gets the input as its argument, and its return value is the output.

A tool that exits with a nonzero code for a normal result, such as `pytest`
when some tests fail, needs a wrapper line. The wrapper line writes the exit
code as the output, and the script exits 0:

```toml
[run]
entry = ["sh", "run.sh"]
```

```sh
# run.sh
pytest -q > log.txt
echo "{\"output\": {\"exit_code\": $?}}" > "$ASCENT_OUTPUT"
```

The metric then reads `output["exit_code"]`. The metric box holds no
candidate files, so put each number the metric needs into the output.

## Cases: `[data] cases`

`[data] cases` names the only source of a repository run's cases. The file
holds one case on each line, as a JSON object:

```json
{"input": "Paris, the capital", "expected": [48.85, 2.35]}
{"input": {"text": "Rome"}}
```

- `input` is required. It goes to the candidate: a function entry gets it as
  its argument, and a command entry reads it from the `ASCENT_INPUT` file.
- `expected` is optional. Only the metric gets it, in the whole case.
- Blank lines are skipped. A UTF-8 byte-order mark at the start is allowed.
- The file is hidden like the `[metric]` files (see "What is hidden"). The
  optimization agent and the candidate never see it. Only the parent's
  scorer reads it.
- The run shuffles the cases with its seed and splits them 70/15/15 into
  train, validation and test. The same file and seed give the same split.
  `[data] split` is not supported.
- A run that uploads its own cases for a repository with `[data]` fails:
  "This repo defines its cases in ascent.toml. Remove the uploaded cases."
- Without `[data]`, the run uses its uploaded cases, if any. With no cases at
  all, the candidate runs once with no input (`None`, or `{"input": null}`
  in the `ASCENT_INPUT` file),
  and the metric judges that one run.

## What is hidden

Only the parent's scorer reads the scoring side of the repository. Staging
takes these paths out of the optimization agent's tree, and the candidate's
box holds none of them while version code runs:

- `ascent.toml` itself.
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

`ascent.toml` is always hidden, also when a visible setting names it. The
file is refused in these cases:

- A visible setting names a hidden path or a path inside one. The visible
  settings are the `instructions` file, an `illegal` file of `[proposer]`,
  `[run]` or a `[target.<name>]`, and the `mcp` file of one of those roles.
  The hidden paths here are `ascent.toml`, a `[metric] files` path, the
  exploits folder, the reference and the cases file.
- The `[run] entry` names a file that is in the hidden list or inside one
  of its paths. This includes `ascent.toml`, the `[metric] illegal` file
  and the `[metric] mcp` file.
- An editable path is in the hidden list or inside one of its paths. This
  also includes `ascent.toml`, the `[metric] illegal` file and the
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
a hidden file. A version's edit to a hidden path, `ascent.toml` included,
is dropped from the result.

The optimization agent never reads `ascent.toml`. It reads its brief,
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
  repository has `.optimize/metric-requirements.txt` or the file sets
  `[metric] setup`. It installs only the packages in that file. When the
  file sets `[metric] setup`, the box then puts the `[metric] files` in,
  runs the command, and removes the metric files again before it saves its
  state. The build output stays in the saved state. The box reaches only the
  package registries. It gets no secret, no service and no version file. The
  parent saves this second state. It holds no repository code and no
  `[metric] files`, only the installed packages and the build output.
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
version file, so every contract needs a `[run]` section or a
`[target.<name>]` stage whose outputs the metric scores. This rule holds also
when the file has no `[metric]` section, because the metric code can come
with the run request. A contract with neither fails setup, because the
metric would give each version the same score.

In Skynet's default image, the box user can be root. The boxes depend on the
microVM boundary between boxes, not on file permissions inside a box. A user
image must set a non-root numeric `USER` (see "Box images" below).

## Box images: `image` and `dockerfile`

By default, every box uses Skynet's sandbox image. `[run]` and `[metric]`
can each name their own image. The `[run]` image is the image of the setup
box and of each candidate box. The `[metric]` image is the image of the
metric setup box and of each metric box. Set one of two keys in a role:

- `image`: an image pinned by its digest, `name@sha256:<64 hex
  characters>`. A tag alone fails the setup check, because a tag can change
  during a run.
- `dockerfile`: the repository path of a Dockerfile. Its folder is the build
  context. The run builds the image one time, before the first version.

The rules:

- **The image is frozen.** The run builds from the pinned start commit, so
  no version edit reaches the image. `ascent.toml` protects each Dockerfile
  automatically. No editable path can overlap the build context, unless a
  protected path covers that overlap. Protect the folder, or put the
  Dockerfile in a folder with no editable path. A patch that changes the
  Dockerfile or a file of its context is rejected.
- **Each image is checked first.** Before the first version, the run opens
  one offline box of each image and runs `ascent check --image --role
  candidate` or `--role metric` in it. A failure stops the run before any
  version, and the error lists the failed checks. The metric check also
  gives the absolute path of the image's Python. The metric runs with that
  Python.
- **A build is reused.** The run computes an input hash from the Dockerfile,
  its context and the builder. A later run of the same user with the same
  inputs uses the same image and builds nothing. A failed build stops the
  run and shows the end of the build log.
- **A resumed run keeps its images.** The run pins its images when it
  starts. A resume uses the pinned images and builds and checks nothing.
- **A build costs budget.** The run reserves the build's maximum cost first
  and charges the minutes that the build used, also for a failed build.

A minimal metric image:

```dockerfile
# eval/box/Dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends sudo \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir openascent==0.4.5 numpy==2.1.0
RUN useradd --uid 1000 --create-home box \
  && printf 'box ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/box \
  && chmod 0440 /etc/sudoers.d/box
USER 1000
WORKDIR /home/box
```

```toml
[paths]
editable = ["src"]

[metric]
files = ["eval"]
dockerfile = "eval/box/Dockerfile"
```

The image must hold `openascent`, because the check and the metric's runner
use it. The image must also set `USER` to a numeric user id that is not 0,
for example `USER 1000`. The setup check refuses an image with no `USER`, a
root `USER`, or a named `USER` such as `USER app`. Skynet cannot confirm
outside the box that a name is not root, because the image's own
`/etc/passwd` maps the name to a user id. `USER 1000:1000` also works.
The box user must be able to make a folder in the image's working folder,
because Skynet keeps its files there. Set `WORKDIR` to a folder that the box
user owns, for example `/home/box`. With no `WORKDIR`, a hosted box starts in `/`,
and the image check refuses the image. In a `[metric]` image, the box user
must also find `tar`, `gzip`, `split`, `base64`, `cat`, `ls`, `rm`,
`mkdir`, `find`, `xargs` and `touch` on its `PATH`. Skynet runs them to
install and save the metric files. Both images also need `tail`, `head`, `base64` and `tr` in
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, because a
hosted run reads a command's output with them. Both images must also hold
`/usr/bin/setpriv`, `/usr/bin/timeout`, `/bin/bash`, `/bin/cat`, `/bin/rm`
and `/bin/mkdir` at these exact paths. A hosted run starts every box command
and every file upload with them. `python:3.12-slim` has all of these tools.
A `[metric]` image must also install `sudo` and give the box user a
`NOPASSWD` rule for all commands, because the first and last steps of each
file upload run as root. `python:3.12-slim` has no `sudo`. The image check
refuses an image without the six paths on every runtime. A local Docker
run does not use `sudo`, so only a hosted run proves the `sudo` rule.

A `[run]` image needs other tools and more rules, because Skynet runs its
own commands in the setup box and in each candidate box:

- The box user must find `env`, `git`, `tar`, `gzip`, `split`, `base64`,
  `cat`, `ls`, `cp`, `mv`, `rm`, `mkdir`, `find`, `grep`, `sed`, `mktemp`,
  `chmod`, `bash`, `setsid`, `nohup`, `seq` and `sleep` on its `PATH`.
  Skynet runs them to unpack, save and score each version, and to copy,
  start and stop its Compose services. `python:3.12-slim` has all
  of them except `git`.
- Root must be able to run a command with the run's variables, and to run
  the first and last steps of each file upload. On a hosted run, Skynet
  starts its root commands through the image's `sudo`. Install
  `sudo`, give the box user a `NOPASSWD` rule for all commands, turn off
  `env_reset`, turn on `always_set_home` so that root does not use the box
  user's home, and set `secure_path` to
  `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`. Every
  other command starts with `no_new_privs`, so the version's code cannot
  use the rule.
- `/usr/local/bin/python3` must be a trusted Python. The error of the
  image check lists the file and folder rules.
- The box user must not be able to change the `sudo` that runs root's
  commands, or the files that `sudo` reads as root (SKY-912). Root must
  own each `sudo` in a folder of `secure_path`, and the first `sudo` on
  the image `PATH`. Root must also own each folder of the image `PATH` up
  to and with the folder that holds that `sudo`. Root must own these files
  when they exist: `/etc/sudo.conf`, `/etc/sudoers`, `/etc/environment`,
  `/etc/default/locale`, `/etc/pam.conf`, `/etc/passwd`, `/etc/group`,
  `/etc/shadow`, `/etc/gshadow` and `/etc/login.defs`. Ubuntu's
  `pam_umask` reads `/etc/login.defs` as root in the `sudo` stack, so a
  box-writable copy could set the umask of root's steps. Root must own
  `/etc/sudoers.d`,
  `/etc/pam.d`, `/usr/lib/pam.d`, `/usr/lib/security` and `/etc/security`
  when they exist, and every entry in them. Debian's Linux-PAM reads a PAM
  file from `/usr/lib/pam.d` when `/etc/pam.d` does not have it, and it
  also names `/usr/lib/security` as a module folder. Group and others
  must not be able to write any of them. Linux-PAM does not check who
  owns its files, so the image check does. Ubuntu's `sudo` PAM stack
  reads `/etc/environment` and
  `/etc/default/locale` into root's environment. So the check also reads
  the variable names in those two files and in
  `/etc/security/pam_env.conf`. It refuses there the same names that it
  refuses in the image `ENV`, such as `LD_PRELOAD` or a locale that is not
  C, POSIX or UTF-8. Any locale set in `pam_env.conf` is refused, because
  its values can expand other variables. The check also refuses a line in
  these files that ends in a backslash, a variable name that is not
  letters, digits and underscores, and a line with a character that is not
  printable ASCII, such as a carriage return. Linux-PAM ends a line only
  at a line feed, so a carriage return could hide a path from the check.
  It refuses an image that has a `pam_env` drop-in folder or a vendor
  copy, such as
  `/etc/security/pam_env.conf.d`, `/etc/environment.d` or
  `/usr/etc/environment`, because it does not read them. It also refuses
  an image when it cannot search the folder of one of those paths.
  systemd ships `/usr/lib/environment.d` with only a link to
  `/etc/environment`, so the check accepts that folder when each entry
  in it resolves to `/etc/environment`. In `/etc/pam.conf`, `/etc/pam.d`
  and `/usr/lib/pam.d`, a `pam_env` line in a stack that `sudo` reaches
  may name only those three files with `envfile=` or `conffile=`, and may
  not turn on `user_readenv`, which reads a file in the target user's
  home (root's). The check reads these options on every line of those
  stacks, also inside `[...]` brackets, because another module name can
  load `pam_env`. Stock `sshd` turns on `user_readenv`, but `sudo` does
  not reach it. The check splits words
  as Linux-PAM does: only at a space or a tab, and a `[...]` argument ends
  at its first `]` that has no `\` before it, even across spaces. The
  check refuses a `[...]` argument with no closing bracket. It also
  refuses a PAM line that ends in a backslash outside a comment, because
  Linux-PAM versions join such a line with the next one in different ways.
  It refuses a PAM line with a character that is not printable ASCII,
  except a tab.
  No line in these PAM files may name any other path, such as a module or
  an `@include` file outside the module folders. The only other path
  allowed is `/run/motd.dynamic`, which stock images name. The check
  refuses `pam_exec`, `pam_script` and `pam_python`, which run a program
  that an argument names, even by a relative name. It also refuses a
  module name that is a link to one of them in a module folder. It
  refuses `pam_motd`, or a link to it, in a stack that `sudo` reaches: the
  `sudo` and `sudo-i` stacks, `other`, and every file that they include.
  Debian's `pam_motd` runs the scripts in `/etc/update-motd.d` as root,
  and those scripts run programs that the check does not walk. Stock
  images name `pam_motd` only in `login` and `sshd`, which `sudo` does not
  reach. The
  check does not read a sudoers `pam_service` setting, which could start
  another stack. A box user who can change the account files can join
  the `sudo` group or set a root
  password. Others need not read `/etc/shadow` or `/etc/gshadow`, which
  are often mode 0640 or 0000. An image with no `/etc/pam.d` passes;
  Linux-PAM then reads `/etc/pam.conf`, which the check reads too.
- The check also needs these rules:
  - Others must be able to read each `sudo`, and to search each folder of
    the image `PATH` up to and with the folder that holds the first
    `sudo`.
  - Others must be able to run the first `sudo` on the image `PATH`. A
    `PATH` lookup by a non-root user skips a file that the user cannot
    run.
  - Each `sudo` must resolve to an ELF file, not a script.
  - Others must be able to list `/etc/sudoers.d`, `/etc/pam.d`,
    `/usr/lib/pam.d`, `/usr/lib/security` and `/etc/security`.
  - Each entry in those folders must be a regular file, or a link to one.
    `/etc/security` may also hold one level of such folders, for example
    `limits.d`.

  The Debian and Ubuntu `sudo` packages meet these rules. Red Hat images
  do not: their `sudo` is mode 4111 and `/etc/sudoers.d` is 0750. Alpine
  images with `sudo` do not either: their `/etc/sudoers.d` is 0750.

The image check refuses an image that breaks one of these rules, before
the first version. A minimal `[run]` image:

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git sudo \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir openascent==0.4.5
RUN useradd --uid 1000 --create-home box \
  && printf 'Defaults always_set_home\nDefaults !env_reset\nDefaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\nbox ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/box \
  && chmod 0440 /etc/sudoers.d/box
USER 1000
WORKDIR /home/box
```

A hosted run copies the user image into Vercel's registry before its first
box, and the run budget pays for the copy. The box runs linux/amd64, so an
index gives only its linux/amd64 image. One limit stays: a hardening run
does not support a user image yet.

## The metric: `[metric] entry`

`[metric] entry` names the metric. It has two forms:

- A Python function, `"path.py:function"`. The default is
  `"metric.py:score"`, so a file without `entry` scores as before. The code
  is the metric code of the run (the `metric_code` of the scorer), and the
  function is the one that `entry` names. The calls are the same as above.
- A command list in any language, for example `["bash", "eval/metric.sh"]`
  or `["node", "eval/metric.mjs"]`. Each part is a non-empty string. One part
  must name a file under `[metric] files`, because only those files reach the
  metric box. The command needs a `[run]` section, because it scores the
  `[run]` output. The run needs no metric code. When the run sends metric
  code anyway, the command ignores it.

Any other value fails the setup check, with one message that shows the two
forms.

A command runs in the metric box, in the folder that holds the `[metric]
files`, through the `call_metric` function of the pinned
[OpenAscent](https://pypi.org/project/openascent/) package. It gets two
environment variables:

- `ASCENT_INPUT`: the path of a JSON file to read. It holds one object:
  - `input` (any JSON value): the case's `input`.
  - `output` (any JSON value): what the `[run]` entry returned for the case.
    It is `null` when nothing ran.
  - `expected` (any JSON value, optional): the case's `expected`, when the
    case has one.
  - `elapsed_s` (number, optional): the run time of the `[run]` entry in
    seconds, measured outside the version's code. The object has no
    `elapsed_s` when the time is not known. See "Run time for the metric"
    below.
- `ASCENT_OUTPUT`: the path where the command writes its answer as JSON.

The answer is one object, the same form as a Python metric's answer:

- `score` (finite number, required): the score of the version.
- `feedback` (string, optional): why the version got this score.
- `scores` (object, optional): a named score for each name, each one
  `{"score": <finite number>, "feedback": <string, optional>}`.

Any other key is an error. The command must exit with code 0. Its stdout is
not read. A failure gives the version no score, and the message names the
cause: the command could not start (for example a missing file), a time
limit, a nonzero exit code (the message shows the code), no answer file, an
answer file that is not a plain file, an answer that is not valid JSON, or
an answer with no `score` or another wrong form. The stderr of the command
stays in the server log. For an answer that is not valid JSON, the server log
gets the same fixed cause as a `[run]` command, which quotes no part of the
answer file.

The command has the same limits as a Python metric: the same box, network,
services, vCPUs and time limit. The metric box holds no version code, so
the command cannot start the candidate again. It reaches only the
`[metric]` services and hosts.

A command reaches the stages, the judge model and the run log at local URLs
that the metric box serves on `127.0.0.1` only. These URLs use the
OpenAscent wire form, so the `openascent` helpers `call`, `llm` and `log`
work in a command too. Each answer is JSON. An error answer has a status
that is not 2xx and the body `{"error": "<text>"}`.

- `ASCENT_STAGE_<NAME>_URL`: one variable for each `[target.<name>]` stage.
  In `<NAME>`, each ASCII letter is upper case, each ASCII digit stays, and
  each other character is `_`. Two stages with the same `<NAME>` (such as
  `a-b` and `a_b`) get no variable, so a call to either fails as an
  unknown stage. POST `{"input": <any JSON>}`. The answer is
  `{"output": <the stage's output>}`. An unknown stage, a failed stage, a
  stage with no valid output, a stage time-out and a body with another key
  each give an error answer.
- `ASCENT_MODEL_URL`, `ASCENT_MODEL` and `ASCENT_MODEL_KEY`: only when the
  run has a judge model. POST an OpenAI chat request to
  `$ASCENT_MODEL_URL/chat/completions`, with `model` set to `$ASCENT_MODEL`.
  The request takes `messages` and, optionally, `max_tokens`, `temperature`
  and `reasoning_effort`. A request that names another model, asks for a
  stream or has another key gets an error answer. `ASCENT_MODEL_KEY` is
  empty, because the box adds the key itself. Each call is metered like a
  call from a Python metric.
- `ASCENT_LOG_URL`: POST `{"event": "<name>", "fields": {...}}`. The run log
  gets the same event as `log()` in a Python metric. The answer is `{}`.

The command reaches the model and the stages only through these URLs. Its
environment holds no model key, no model mailbox address
(`APP_BUDGET_RELAY_URL`, the box's own `ASCENT_MODEL_URL`, `OPENAI_BASE_URL`,
`ANTHROPIC_BASE_URL`) and no tool relay token (`APP_TOOL_RELAY_TOKEN`), also
not in the old `SKYNET_` spelling. The box command that starts the runner has
no model key in its environment either. The scorer writes the key to a file
in the call folder, and the runner reads and deletes the file before the
command starts. The runner also blocks same-user reads of its own memory
and environment, so the command cannot read them from `/proc`. No process
that starts the metric has the model key in its environment. On a Vercel
box, the processes above the runner also have no relay values. In a
contained session, the guest process that runs the session starts with the
relay address in its environment. The guest blocks same-user reads of its
memory and environment before it starts a command, so the metric command
cannot read them from `/proc` either (SKY-880). If the kernel refuses the
flag, the guest stops. The runner does the same in every mode, so a stage
or `[run]` call that runs in the same session is not readable either. For
the runner the flag is best effort: if the kernel refuses it, the runner
continues. The guest keeps the tool relay token in its own memory, not in
its environment. So no command of a contained session gets the token, and
no child that copies the guest's environment gets it either (a proposer,
`git apply`, a served program). Only the guest's own ReAct tools use the
token. A command stage or a proposer still has the mailbox address in its
environment, and so does the shell that starts a command stage's runner,
because they reach models through it. Hiding the address is defense in
depth: the command can also find the mailbox port in `/proc/net/tcp`.

A Python metric gets none of these variables, because it imports the helpers.

A bash metric that calls a stage and asks the judge model:

```bash
#!/usr/bin/env bash
set -euo pipefail
input=$(jq -c '{input: .output}' "$ASCENT_INPUT")
checked=$(curl -sf --noproxy '*' -X POST -d "$input" "$ASCENT_STAGE_CHECK_URL" | jq -c .output)
verdict=$(jq -nc --arg model "$ASCENT_MODEL" --argjson checked "$checked" \
  '{model: $model, messages: [{role: "user", content: "Rate 0 to 1: \($checked)"}]}' |
  curl -sf --noproxy '*' -X POST -d @- "$ASCENT_MODEL_URL/chat/completions" |
  jq -r '.choices[0].message.content')
curl -sf --noproxy '*' -X POST -d '{"event": "metric.judged", "fields": {}}' "$ASCENT_LOG_URL" >/dev/null
jq -nc --argjson score "$verdict" '{score: $score}' > "$ASCENT_OUTPUT"
```

A bash metric that scores an exact match:

```bash
#!/usr/bin/env bash
set -euo pipefail
python3 - <<'EOF'
import json, os
with open(os.environ["ASCENT_INPUT"]) as f:
    data = json.load(f)
ok = data["output"] == data.get("expected")
with open(os.environ["ASCENT_OUTPUT"], "w") as f:
    json.dump({"score": 1.0 if ok else 0.0, "feedback": "exact match" if ok else "different"}, f)
EOF
```

The same metric in Node:

```js
// eval/metric.mjs
import { readFileSync, writeFileSync } from "node:fs";

const data = JSON.parse(readFileSync(process.env.ASCENT_INPUT, "utf8"));
const ok = JSON.stringify(data.output) === JSON.stringify(data.expected);
writeFileSync(process.env.ASCENT_OUTPUT, JSON.stringify({ score: ok ? 1 : 0 }));
```

## Build the metric: `[metric] setup`

`[metric] setup` is one shell command that builds the metric, for example
for a metric in a compiled language. It is text of 1 to 300 characters, the
same limit as `[setup] command`. Another type, or a text that starts or
ends with a space or a line break, fails the setup check.

The command runs one time for each run, in the metric setup box, before the
first metric call. It runs in the folder that holds the `[metric] files`
paths, with the repository root layout, the same folder where the
`[metric] entry` command runs. It does not run inside one of those paths.
With `files = ["eval"]`, start the command with `cd eval`. The box reaches
only the package registries. It gets no secret, no service and no version
file. The time limit is 30 minutes, and the box time counts in the run's
cost. A command that fails or runs out of time stops the run before the
first version. The error shows the last lines of the build output.

Each metric box opens from the state that the metric setup box saved. The
saved state can be the box's working folder only, so a file that the build
writes outside the working folder may not be kept. Build into the folder of
the metric files, for example a `target/` or `bin/` folder next to the
source.

A file that the build writes inside the working folder is kept, and it
enters every metric box. In local Docker boxes and the local runtime, `HOME`
is the working folder. The sandbox image sets `CARGO_HOME=/tmp/cargo`,
`GOPATH=/tmp/go` and `GOCACHE=/tmp/go-build`, so in a sandbox image box the
Rust and Go caches go to `/tmp`. The local runtime does not set them, so
there `~/.cargo` and `~/go` (with `~/go/pkg/mod`) are in the working folder
and stay in the saved state. Keep the caches small: set `CARGO_HOME` or
`GOMODCACHE` to a folder under `/tmp` in the command, or remove the caches at
the end of the command.

After the build, the metric setup box removes the `[metric] files` again.
Only the build output stays in the saved state, so no metric box holds the
metric source before the version's box ends. The metric box then puts the
`[metric] files` in the folder again, and the built files stay next to them.
Do not let the build write to the path of one of the `[metric] files`. The
metric box puts the original file back at that path, so the built file is
lost.

A Rust metric:

```toml
[metric]
files = ["eval"]
setup = "cd eval && cargo build --release"
entry = ["eval/target/release/metric"]
```

A Go metric:

```toml
[metric]
files = ["eval"]
setup = "cd eval && go build -o metric ./cmd/metric"
entry = ["eval/metric"]
```

The compilers must be in the sandbox image. The package registry hosts
include `crates.io` and `proxy.golang.org`, so the build can fetch its
crates and modules. The metric box does not need those downloads after the
build. See above for where the caches go and how to keep them out of the
saved state.

Metric hardening needs a Python metric. A hardening job for a command
metric stops with the message "needs a Python metric".

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
that stdout: the entry's prints go to stderr, and the supervisor reads the
entry's answer from a file. The time includes the start of the Python child
process that runs the entry, for both entry forms. It does not include setup
or the metric. The
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
trust; the clock stays in the supervisor. A command entry's own process
loads OpenAscent, so it starts with the supervisor's interpreter. The
command itself still finds `python3` on `PATH`. The shell that starts the
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
`setpriv --no-new-privs --inh-caps=-all --ambient-caps=-all`, so the same
rule applies to them. The drop also removes the capabilities that Vercel
gives every command, so the box user cannot write root-owned files
(SKY-901).
A root command finds programs only in root-owned folders, and no program
file in them is writable by the box user. The box user can write
`/app/.venv/bin`, so the image's `sudo` rule and the provider both set
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
- Vercel's in-box agent starts `sudo` itself for a `sudo: true` command. If
  it finds `sudo` by a `PATH` lookup, a `sudo` that the box user plants in
  `/app/.venv/bin` would get root. The image puts a root-only guard folder,
  `/usr/local/libexec/skynet-path-guard`, first on its `PATH` and in its
  login shell `PATH`. That folder holds only a link to `/usr/bin/sudo`.
  A real staging box (SKY-912, Debian 13) showed that the agent starts
  `sudo` by its bare name, and the process ran `/usr/bin/sudo`. A
  box-user `sudo` first on the command's own `PATH` did not run. A root
  `sudo` in `/usr/local/bin` did not run either, although that folder
  comes before `/usr/bin` on root's `PATH`. So the agent runs a fixed path
  or looks `sudo` up on its own `PATH`, probably the image `PATH`. The
  check did not prove which. So the image check walks both: every `sudo`
  in a folder of root's `PATH`, and the image `PATH` up to its first
  `sudo`. It also walks the `sudo`, PAM and account files:
  `/etc/sudo.conf`, `/etc/sudoers`, `/etc/environment`,
  `/etc/default/locale`, `/etc/pam.conf`, `/etc/passwd`, `/etc/group`,
  `/etc/shadow`, `/etc/gshadow`, `/etc/login.defs`, and each entry in
  `/etc/sudoers.d`, `/etc/pam.d`, `/usr/lib/pam.d`, `/usr/lib/security` and
  `/etc/security`. It does not
  walk a file that sudoers includes from another folder, or a `sudo.conf`
  `sudoers_file` setting. Sudo itself refuses such a file when root does
  not own it, or when others can write it. The check does not parse
  sudoers, so it does not walk a file that a sudoers setting names, such
  as `Defaults env_file=` or `restricted_env_file=`. Sudo reads such a
  file with no owner check, so a box-owned `env_file` can put
  `LD_PRELOAD` into root's command. The PAM configuration may name no path
  outside a short stock list, so PAM loads modules only from its module
  folders. The PAM modules below the loader folders get only
  the loader-folder check, which needs others to be able to read them.
  The `sudo` plugins, such as `sudoers.so` and `libsudo_util`, also get
  only the loader-folder check. They get it when the `sudo` RUNPATH names
  their folder, as on Debian (`/usr/libexec/sudo`). Sudo itself refuses a
  plugin that root does not own or that others can write.
- The `[run]` entry can plant a `.pth` file in the candidate box's
  site-packages. That file runs only in that candidate box. The box ends
  after the case, and the metric box never gets the file (SKY-398).
- Each case starts from the state that `[setup]` left. A cache that
  `[setup]` fills (for example a download or a model file) makes every case
  faster, but a case leaves nothing for the next case.
- Where the image has no `/usr/local/bin/python3` (for example a local test
  host), the supervisor falls back to `python3` from `PATH`.

A timeout, a crash, a function entry that exits before it returns, or other
`[run]` failure gives the case no score, and the metric does not run. A
command entry that exits with a nonzero code, or writes no valid output file,
also fails (see "Command entries: one call rule"). The runner never makes a
score.
Only the metric makes a score. The failure text goes to the proposer as
feedback. A version with a case that has no score has no score itself:

- It cannot be the best version of the search, and it ranks below every
  version that has a score.
- Every engine applies this rule during the search too. The version never
  replaces a version that has a score and never leads the frontier. It can
  still be a parent for the next step in two cases. GEPA always starts from
  the starting version, also when that version failed a case. AutoSaddler
  keeps every parent when the `[run]` entry of every accepted version failed.
- A metric error is different during the search. The metric fails, not your
  program, so the case gets the version's mean over its other cases. A metric
  that fails one case for every version then still lets the search rank the
  versions.
- A metric error in the final run also gives the version no score.
- A version with a metric error stays in the version tree with no score,
  so its child versions keep their parent.
- It reaches no `[outcome] target`, passes no limit and opens no pull
  request.
- The starting version can fail a case. Then a version that passes every
  case counts as better and opens a pull request. The result shows no
  starting score and no improvement number.
- When no version has a score, the run keeps the starting version and says
  why.

If a failure must count as a number, catch the failure in your program and
write an output that the metric can score. The metric then gives the number.

Release note (SKY-751): before this change, a failed `[run]` case scored 0.
A run whose starting code fails some cases, with the goal to fail fewer
cases, now gets no score for each version that still fails a case. The
search then loses its step-by-step progress signal. To keep that signal,
catch the failure in the program, as above, and let the metric score it.

## What is enforced

| Setting | Enforcement |
| --- | --- |
| `[paths] editable` | The parent checks every version before it is scored or published. A path outside the approved editable paths fails the setup check. |
| `[paths] protected` | The parent rejects a version that changes a protected path, in its patch or while its code runs. A write that the code undoes before it exits also counts. The rejection names the path and the rule, with secret values hidden. `ascent.toml` itself is always protected and hidden. |
| `[proposer] network` | The proposer's box reaches the model relay and nothing else, except the hosts below. With no `network` key, it also reaches the package registries: the account's saved Python index (default `pypi.org` and `files.pythonhosted.org`) and `registry.npmjs.org`. A custom index replaces the PyPI hosts with the index's own host. `network = []` gives the relay only. `network = ["docs.python.org"]` gives the relay, the registries and that host. The run record shows the effective hosts under `roles`. |
| `[proposer] entry`, `[proposer] setup` | The command runs as the run's proposer, in the proposer's box, in the repository checkout, under the `[proposer]` rules. The repository files that `entry` and `setup` name by exact path are protected. Put the proposer's folder in `[paths] protected` to protect the files that it imports. A proposer that the run names replaces it. Only a single AutoResearch or Meta-Harness run can use it today; every other run refuses it. |
| `[setup] command` | Runs once on the starting commit, in its own box, with package-registry network and no secrets. No version runs in that box. |
| `[metric] setup` | Runs once in the metric setup box, in the folder of the metric files, with package-registry network and no secrets. No version file is in that box. A failure stops the run before the first version. |
| `[run] network`, `[metric] network` | The candidate box or the metric box reaches only these hosts, besides the relay. With no `network` key, or `network = []`, the box is offline except for the relay. |
| `[run] image`, `[run] dockerfile`, `[metric] image`, `[metric] dockerfile` | The box opens from this image. The run builds a Dockerfile once from the start commit and checks each image with `ascent check --image` before the first version. A failure stops the run. See "Box images: image and dockerfile" above. |
| `[run] vcpus`, `[metric] vcpus` | The size of the candidate box or the metric box: `1`, or an even number from 2 to 32. Another value fails the setup check. With no key, the box gets the run's default size. |
| `instructions` | Not enforced. The agent reads the file in its brief. The scorer decides the score. |
| `[proposer] illegal` | Enforced by judge models. See "Plain-language rules" below. |
| Rule files | The `instructions` file and every `illegal` file (in `[proposer]`, `[run]`, `[metric]` and `[target.<name>]`) are protected, so no version can change its own rules. |

## What fails the setup check

- A contract with no `[run]` section and no `[target.<name>]` stage, with or
  without a `[metric]` section. The message says to add one of them.
- A `services` list in a `[target.<name>]`, or a `network` host list in a
  `[target.<name>]`. Runs cannot use these settings yet. `network = []` in a
  stage passes, because a stage box is already offline except for the relay.
- A `[metric] entry` that is not `"path.py:function"` and not a list of
  non-empty strings. A command `entry` that runs no file from `[metric]
  files`, or that comes without a `[run]` section. A Python `entry` when the
  run has no metric code.
- A `tools` key in `[proposer]`, `[run]`, `[metric]` or `[target.<name>]`.
  The message says to list the MCP servers in a `.mcp.json` file and set
  `mcp = "<path>"` instead.
- A `[proposer] entry` or `setup` that is not a list of non-empty strings,
  or a part with a control character, or that contains `__ASCENT_MODEL_KEY__`
  or an older key token. A `setup` without `entry`. An `entry` together with
  `[proposer] mcp`. An `entry` in any run that is not a single AutoResearch
  or Meta-Harness run.
- A proposer setting that the run's engine does not apply (SKY-728). Every
  run uses the worker's engine loop, and each engine runs only its built-in
  proposer there. The setup check refuses these settings before paid work,
  and the message names the engine and each setting:
  - Every engine refuses a `harness` other than the default (such as
    `opencode`, `pi` or `aider`), a `run_command`, an `install_command` and
    a `host_cli` in the run request.
  - GEPA, Best-of-N and AutoSaddler make model calls only. They also refuse
    a `max_tool_calls` other than the default, `[proposer] mcp` and
    `[proposer] services`. They apply the reasoning effort.
  - Only AutoResearch applies `ralph` and `max_no_eval_seconds`, and only
    Meta-Harness applies `max_candidates_per_iter`. Auto applies all three.
    Every other engine refuses a value other than the default (`ralph =
    true`, `max_no_eval_seconds = 1800`, `max_candidates_per_iter = 3`).
  - ShinkaEvolve refuses the same settings as GEPA. With the `agent`
    editor, it applies `max_tool_calls`, `[proposer] mcp` and the reasoning
    effort, and it refuses `[proposer] services`. It does not support a
    `[proposer] mcp` server with `headers` yet, so it refuses one. MCP
    servers with no headers still work. SKY-840 lifts this limit.
  - AutoResearch and Meta-Harness, and Auto, which runs their lanes, apply
    `max_tool_calls` and `[proposer] services`. They refuse `[proposer] mcp`
    and a reasoning effort.
- A service or MCP server that breaks the secret rules below.
- An unknown section, or a setting that the format does not define in any
  section. A missing `version`, a `version` that is not `1`, or invalid TOML.
  A `tool` value or a `tool.<name>` value that is not a table.
- A path that leaves the repository, names `.git`, or protects the whole tree.
- A path with `*`, `?` or `[`. Paths are literal; patterns are not supported,
  so name the files or folders.
- An empty `[paths] editable` list, or protected paths that cover every
  editable path. Such a run could not change anything. This includes a run
  without the file whose only approved editable path is `ascent.toml`,
  because `ascent.toml` is always protected.
- An `ascent.toml` that is a link or a folder, or a file larger than 64 KiB.
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
and MCP servers. When the run uses the file's proposer, it also holds
`proposer_entry` and `proposer_setup`.
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
- Only the AutoResearch and Meta-Harness tool loops (also in Auto) reach
  the services of `[proposer] services`. Skynet checks each call against the
  proposer's own list, fills the secrets in the parent, and returns
  redirects without following them. Every other engine refuses
  `[proposer] services` at the setup check (see "What fails the setup
  check"). The `[proposer] entry` command of AutoResearch and Meta-Harness
  does not get the services. A single run of these engines with `entry` and
  `[proposer] services` fails the setup check, and the message names
  `services`.
- `[proposer]`, `[run]`, `[metric]` and each `[target.<name>]` can name a
  standard `.mcp.json` file with `mcp = "<path>"`. A role sees only the
  servers of its own file, so the proposer gets none of the metric's
  servers. Skynet reads the files from the same commit as `ascent.toml`,
  checks them with the same secret rules, lists their servers in the
  environment record, and protects them like rule files.
- A `[target.<name>]` stage reaches the servers of its own `mcp` file
  through its box's relay (SKY-611). The stage box's copy of the file lists
  only that stage's servers, each with a loopback url, and holds no secret.
  The stage also gets `ASCENT_MCP_<NAME>_URL` and `ASCENT_MCP_CONFIG`.
  Skynet checks each call against that stage's own file, so a stage cannot
  reach the proposer's, the scorer's or another stage's servers.
- A stage with an `mcp` file runs calls in parallel, up to the run's
  concurrency, like any other stage (SKY-624). Each call serves its own
  relay on its own loopback port. Each call gets its own copy of the MCP
  file with that port, and `ASCENT_MCP_CONFIG` names that copy. Read the
  servers from `ASCENT_MCP_CONFIG` or `ASCENT_MCP_<NAME>_URL`. The copy of
  the file in the stage's tree names a fixed port that no call serves.
- Only http servers are supported now. Each server has a `url` and goes
  through Skynet's relay. A stdio server (`command`, `args`, `env`) fails
  the setup check before any sandbox starts. A `type` must be `http`,
  `streamable-http` or `sse`, in lowercase; any other `type` fails.
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
- A `[proposer] mcp` server with `headers` owns its host alone. The edge
  adds the headers only on the server's url path, but a path such as
  `/team/../web` can still reach them. So the ShinkaEvolve agent editor
  refuses to start when:
  - two servers with `headers` use one host;
  - a server with no `headers` uses the host of a server with `headers`;
  - the proposer's network reaches that host: a `[proposer] network` entry,
    a package registry or the account's saved package index. A suffix entry
    such as `*.example.com` counts when it matches the host, and also when
    the host is `example.com` itself.

  The check ignores case and a trailing dot, so `MCP.example.com.` is
  `mcp.example.com`. To fix it, give each server with `headers` its own
  host, or remove its `headers`, or remove the host from `[proposer]
  network` or the saved package index.
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
