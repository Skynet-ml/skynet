"""Run the pinned upstream Best-of-N engine through Skynet's model transport."""

from __future__ import annotations

import math
import posixpath
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.engines.best_of_n import BestOfNEngine as UpstreamBestOfN

from ....exceptions import ServiceError
from ..budget_stop import BudgetReached
from .feedback import emit_candidate
from .harness_bridge import hashed_kind
from .protocol import EngineContext, EvalServer, RepoSeed, Result, Task
from .upstream import local_result, reflection_endpoint, upstream_server

# Top train samples re-scored on the validation split; each costs one scorer
# run per validation case, so the cap keeps sampling the bulk of the budget.
_MAX_FINALISTS = 3
# The whole repository brief: path rules, file blocks, notices and the line
# breaks between them. Upstream sends the brief in every sample's prompt, so
# the cap bounds each model call.
_BRIEF_MAX_BYTES = 64_000
# A repository can name thousands of paths in one rule (for example every LFS
# file as read-only), so each rule lists a few and counts the rest.
_BRIEF_MAX_PATHS = 50
# Paths have no length bound, so each rule's list also stops at a byte budget:
# three full rules still leave most of the brief for file content.
_BRIEF_MAX_PATH_BYTES = _BRIEF_MAX_BYTES // 8
# The tree entries carry no size, so a repository of many large or binary files
# would make the brief read about a thousand whole blobs on each resume.
_BRIEF_MAX_READS = 256
# Upstream's reply parser ends the candidate at the first run of three
# backticks anywhere in the reply, also inside a line, so a repository diff
# spells each such run as a token that Skynet turns back before scoring. A
# literal ``<<`` is doubled, so file text that looks like a token stays text
# and the round trip is exact. Both patterns share one run bound, so each
# token the encoder writes decodes; a longer run splits into several tokens.
# The reply is untrusted: the bound keeps a token of 16 bytes from decoding to
# more than 99, so the decoded diff stays a few times the reply's size, and a
# token above the bound stays text.
_ESCAPE = re.compile("<<|`{3,99}")
_UNESCAPE = re.compile(r"<<<<|<<(\d{1,2}) backticks>>")
# The model writes the hunk header, so its counts are untrusted: the digits are
# bounded, and a gap larger than a few stripped lines gets no padding at all.
_HUNK_HEADER = re.compile(r"^@@ -\d{1,9}(?:,(\d{1,9}))? \+\d{1,9}(?:,(\d{1,9}))? @@", re.MULTILINE)
_MAX_STRIPPED_LINES = 16


class _SampleStream:
    """Announce each fully scored sample as a root of the run's candidate tree.

    Upstream scores the samples one after another, one scorer run per case,
    so consecutive runs group into samples by count alone; a sample cut short
    by the budget never completes its group and stays out of the tree, and a
    sample with a metric error on any case stays out too.
    """

    def __init__(self, task: Task, server: EvalServer, ctx: EngineContext) -> None:
        """Bind the stream to the cases upstream scores samples on.

        Args:
            task: Optimization inputs; each sample is scored on every case.
            server: Run accounting, for the scorer runs spent per sample.
            ctx: The job's progress sink.
        """
        cases = task.cases
        self._case_ids = {id(example): str(index) for index, example in enumerate(cases)}
        self._per_sample = max(1, len(cases))
        self._server = server
        self._sink = ctx.progress_callback
        self._samples = 0
        self._seen = 0
        self._scores: list[tuple[str, float]] = []
        self._metric_error = False

    def __call__(self, candidate: Any, example: Any, score: float | None) -> None:
        """Record one scored case, announcing the sample once its last case is in.

        Args:
            candidate: The sample being scored.
            example: The case scored, ``None`` in single-task mode.
            score: The case's score, ``None`` after a metric error.
        """
        self._seen += 1
        if score is None:
            self._metric_error = True
        else:
            self._scores.append((self._case_ids.get(id(example), "?"), float(score)))
        if self._seen < self._per_sample:
            return
        scores, self._scores, self._seen = self._scores, [], 0
        metric_error, self._metric_error = self._metric_error, False
        sample, self._samples = self._samples, self._samples + 1
        # A sample with a metric error on any case has no score (SKY-350), the
        # same as the run's final selection treats it, and the candidate event
        # carries a number, so the sample stays out of the tree.
        if metric_error:
            return
        emit_candidate(
            self._sink,
            candidate_id=str(sample),
            parent_id=None,
            generation=0,
            score=sum(value for _, value in scores) / len(scores),
            per_example=[] if example is None else scores,
            candidate=candidate,
            discovered_at_evals=self._server.used,
            iteration=sample,
        )


class _UnescapingServer:
    """Give the scorer a repository sample with its backtick tokens turned back into backticks.

    Every other attribute comes from the wrapped upstream server unchanged.
    """

    def __init__(self, server: Any) -> None:
        """Wrap the upstream server that upstream's sampling loop scores on.

        Args:
            server: The upstream eval server.
        """
        self._server = server

    def __getattr__(self, name: str) -> Any:
        """Return the wrapped server's attribute.

        Args:
            name: The attribute name.

        Returns:
            The attribute.
        """
        return getattr(self._server, name)

    def evaluate(self, candidate: str, example: Any = None, **kwargs: Any) -> tuple[float, dict[str, Any]]:
        """Score the sample as a real diff.

        Args:
            candidate: The sample as upstream parsed it, with backtick tokens.
            example: The case to score on, if any.
            **kwargs: Passed to the wrapped server.

        Returns:
            The wrapped server's score and feedback.
        """
        return self._server.evaluate(_as_patch(candidate), example, **kwargs)


class BestOfNEngine:
    """Adapt the upstream independent-sampling baseline without rewriting its loop."""

    name = "best_of_n"

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Run upstream sampling with the configured metered optimization model.

        Args:
            task: Starting point and optimization examples.
            server: Skynet scorer and budget.
            ctx: Configured model and artifact directory.

        Returns:
            The fully evaluated winner, or an unscored seed if no candidate completes.

        Raises:
            ServiceError: If given named components, a repository run is off the worker engine loop, or a
                seedless run has no completed candidate.
        """
        if not task.str_mode:
            raise ServiceError("Best-of-N supports text starting points only.")
        on_loop = ctx.repo_seed is not None and ctx.read_file is not None and ctx.list_files is not None
        if ctx.native_options is not None and ctx.native_options.repo is not None and not on_loop:
            raise ServiceError("Best-of-N runs a repository only on the worker engine loop.")
        # SKY-721: on the worker engine loop a repository run samples here
        # too, so each sample is one model step with no box open, and only
        # the scorer's commands open boxes. Its candidate is the patch text.
        if on_loop:
            brief = _repository_brief(ctx.repo_seed, ctx.read_file, ctx.list_files)
            task = replace(task, background="\n\n".join(part for part in (task.background, brief) if part))
        sampled = task
        if on_loop and isinstance(task.seed_candidate, str) and task.seed_candidate:
            # Upstream shows the seed in a fence and keeps it as the result when
            # no sample completes, and the result is unescaped below, so the
            # seed goes in encoded like the brief's file text.
            sampled = replace(task, seed_candidate=_escape_backticks(task.seed_candidate))
        finalists = _validation_finalists(task, server)
        sampling = server.lane(server.remaining - finalists * len(task.valset))
        upstream = upstream_server(sampled, sampling, ctx, on_eval=_SampleStream(task, sampling, ctx))
        result = None
        budget_stop = None
        try:
            with reflection_endpoint(ctx) as connection:
                engine = UpstreamBestOfN(
                    OptimizeAnythingConfig(
                        engine=self.name,
                        max_evals=sampling.remaining,
                        stop_at_score=ctx.stop_at_score,
                        engine_config={"model": "openai/app-reflection", "lm_kwargs": connection},
                    )
                )
                result = engine.run(upstream.task, _UnescapingServer(upstream) if on_loop else upstream)
        except BudgetReached as exc:
            budget_stop = exc
        if budget_stop is None:
            budget_stop = next(iter(getattr(upstream, "platform_budget_stops", [])), None)
        if result is None:
            if budget_stop is not None:
                raise budget_stop
            raise ServiceError("Best-of-N did not produce a result.")
        engine.process_result(result, upstream.output_dir)
        adapted = local_result(result, server)
        if on_loop:
            adapted.best_candidate = _as_patch(adapted.best_candidate)
        # Upstream logs a sample with no scored case at -inf; it is not evaluated.
        evaluated = any(math.isfinite(row.get("score", -math.inf)) for row in result.metadata.get("bon_cost_log", []))
        if budget_stop is not None:
            if evaluated:
                budget_stop.result = adapted
                budget_stop.evidence.update(
                    selection_scope="training",
                    final_evaluation_completed=False,
                    final_evaluation_reason="budget_reached",
                )
            raise budget_stop
        if not evaluated:
            if task.seed_candidate is None:
                raise ServiceError("Best-of-N stopped before producing a fully evaluated candidate.")
            adapted.best_score = None
            return adapted
        if finalists:
            return _select_on_validation(task, server, sampling, adapted, finalists)
        return adapted


def _repository_brief(
    seed: RepoSeed,
    read_file: Callable[[str, str], bytes | None],
    list_files: Callable[[str, str], list[str] | None],
) -> str:
    """Brief the model on the repository: the candidate's form, the path rules and the files it may change.

    Upstream's prompt is fixed, so the brief goes in the task's background,
    which upstream sends unchanged in every sample's prompt. The files come
    from the seed tree with no box. The worker host leaves the run's hidden
    paths out of both readers, so the brief never sees a hidden file.

    Args:
        seed: The run's repository seed tree and path rules.
        read_file: Reads one file of a tree; ``None`` for a hidden, folder or missing path.
        list_files: Lists the files under a folder of a tree, sorted; ``None`` when the path is not a folder.

    Returns:
        The brief text.
    """
    # The seed contract: the engine never learns a hidden name (E61), so the
    # path rules leave out each hidden path and each path inside a hidden folder.
    editable_paths = [path for path in map(posixpath.normpath, seed.editable_paths) if _visible(seed, path)]
    fixed = [path for path in map(posixpath.normpath, seed.readonly_paths) if _visible(seed, path)]
    # A constrained path may change, but only within the rules of the repository's constraints file.
    constrained = sorted({path for path in map(posixpath.normpath, seed.constrained_paths) if _visible(seed, path)})
    editable = _path_list(["the whole repository" if path == "." else f"`{path}`" for path in editable_paths])
    lines = [
        "This run optimizes a repository. Each candidate is a unified diff (git diff format) against the "
        "repository root; an empty diff keeps the repository as it is.",
        "Inside the diff, write each run of three or more backticks as `<<N backticks>>`, where N is the "
        "length of the run and at most 99 (split a longer run into several tokens), and write each literal "
        "`<<` as `<<<<`, as the file contents below do. Skynet turns each token back into backticks and each "
        "`<<<<` back into `<<`.",
        f"You may change: {editable or 'nothing'}.",
    ]
    if fixed:
        lines.append("Do not change: " + _path_list([f"`{path}`" for path in fixed]) + ".")
    if constrained:
        lines.append(
            "You may change these paths only within the rules of the constraints file: "
            + _path_list([f"`{path}`" for path in constrained])
            + "."
        )
    shown: dict[str, None] = {}
    for path in (*editable_paths, *constrained):
        for name in list_files(seed.tree_id, path) or [path]:
            if not any(name == other or name.startswith(f"{other}/") for other in fixed):
                shown[name] = None
    names = list(shown)
    # Each line after the first costs its "\n" too, and the room keeps space
    # for the closing count line, so the joined brief stays within the cap.
    closing = f"{len(names)} more files are not shown."
    room = _BRIEF_MAX_BYTES - len("\n".join(lines).encode()) - len(closing.encode()) - 1
    for index, path in enumerate(names):
        # A file gets no read once not even its short notice fits, so a
        # repository of many skipped files reads no file past the room.
        entry = ""
        if index < _BRIEF_MAX_READS and len(_not_text(path).encode()) + 1 <= room:
            data = read_file(seed.tree_id, path)
            if data is None:
                continue
            entry = _file_entry(path, data, room - 1)
        if not entry or len(entry.encode()) + 1 > room:
            lines.append(f"{len(names) - index} more files are not shown.")
            break
        room -= len(entry.encode()) + 1
        lines.append(entry)
    return "\n".join(lines)


def _path_list(labels: list[str]) -> str:
    """Join the first path labels of a rule, and count the rest.

    Args:
        labels: The rule's path labels, in order.

    Returns:
        At most :data:`_BRIEF_MAX_PATHS` labels within :data:`_BRIEF_MAX_PATH_BYTES`, then ``and N more``.
    """
    shown: list[str] = []
    size = 0
    for label in labels[:_BRIEF_MAX_PATHS]:
        size += len(label.encode()) + 2
        if size > _BRIEF_MAX_PATH_BYTES:
            break
        shown.append(label)
    rest = len(labels) - len(shown)
    return ", ".join([*shown, f"and {rest} more"] if rest else shown)


def _not_text(path: str) -> str:
    """Return the notice for a file the brief cannot show as text.

    Args:
        path: The file path.

    Returns:
        The notice line.
    """
    return f"`{path}` is not text, so it is not shown."


def _file_entry(path: str, data: bytes, room: int) -> str:
    """Return a file's brief entry: its fenced, escaped content when it fits the room, else a notice.

    Args:
        path: The file path.
        data: The file bytes.
        room: The bytes left for the entry itself.

    Returns:
        The content block, the binary notice or the "no room" notice.
    """
    no_room = f"`{path}` is not shown: the brief has no room for it."
    # Escaping shortens only a run of 17 or more backticks, so a file larger
    # than the room almost never fits, and the brief does not decode or escape it.
    if len(data) > room:
        return no_room
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _not_text(path)
    # A NUL byte is valid UTF-8, but it marks a binary file.
    if "\x00" in text:
        return _not_text(path)
    # A longer fence here would invite a longer fence in the reply, which upstream cannot parse.
    block = "\n".join([f"Current content of `{path}`:", "```", _escape_backticks(text), "```"])
    if len(block.encode()) <= room:
        return block
    return no_room


def _escape_backticks(text: str) -> str:
    """Spell each run of three or more backticks as its token, and each literal ``<<`` as ``<<<<``.

    Args:
        text: File text.

    Returns:
        The text with no run of three backticks; :func:`_unescape_backticks` gives back the exact text.
    """
    return _ESCAPE.sub(lambda match: "<<<<" if match.group() == "<<" else f"<<{len(match.group())} backticks>>", text)


def _as_patch(text: str) -> str:
    """Turn a repository sample back into a diff that ``git apply`` accepts.

    Upstream's reply parser strips the final newlines, and ``git apply``
    refuses a diff with no final newline as a corrupt patch. The strip also
    drops a blank context line at the end of the last hunk when the model
    wrote it as an empty line, so the hunk falls short of its header.

    Args:
        text: A repository sample as upstream parsed it.

    Returns:
        The sample as a real diff that ends with a newline, or ``""``.
    """
    patch = _unescape_backticks(text)
    if not patch:
        return patch
    if not patch.endswith("\n"):
        patch += "\n"
    return _with_git_headers(patch + "\n" * _stripped_context_lines(patch))


def _stripped_context_lines(patch: str) -> int:
    """Count the empty context lines that the strip took from the end of the last hunk.

    Args:
        patch: A diff that ends with a newline.

    Returns:
        How many lines the last hunk lacks against its header, when only empty context lines can explain the
        gap and the gap is at most :data:`_MAX_STRIPPED_LINES`; otherwise ``0``, so a complete, malformed or
        hostile diff stays as it is.
    """
    headers = list(_HUNK_HEADER.finditer(patch))
    if not headers:
        return 0
    last = headers[-1]
    old_missing = 1 if last.group(1) is None else int(last.group(1))
    new_missing = 1 if last.group(2) is None else int(last.group(2))
    # The first part is the rest of the header line, and the last is the empty text after the final newline.
    for line in patch[last.end() :].split("\n")[1:-1]:
        if line == "" or line.startswith(" "):
            old_missing -= 1
            new_missing -= 1
        elif line.startswith("-"):
            old_missing -= 1
        elif line.startswith("+"):
            new_missing -= 1
        else:
            # A "\\ No newline at end of file" marker lands here: no context line can follow it in a real diff,
            # so the strip never took a line from a hunk that holds one.
            return 0
    return old_missing if old_missing == new_missing and 0 < old_missing <= _MAX_STRIPPED_LINES else 0


def _plain_header_path(old: str, new: str) -> tuple[str, str] | None:
    """Read the one path and the mode line of a plain ``---``/``+++`` header pair.

    Args:
        old: The ``---`` line after its marker.
        new: The ``+++`` line after its marker.

    Returns:
        The path and the mode line it needs (empty for a changed file), or
        ``None`` when the pair names no single ``a/``/``b/`` path.
    """
    old, new = old.split("\t", 1)[0], new.split("\t", 1)[0]
    # git apply drops a CR, end whitespace or a date after a space, which the
    # path checks keep: a CRLF ``a/p.py\r`` would pass a rule on ``p.py`` and
    # still change it. So a path with any whitespace gets no repair.
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in old + new):
        return None
    before = None if old == "/dev/null" else old[2:] if old.startswith("a/") else ""
    after = None if new == "/dev/null" else new[2:] if new.startswith("b/") else ""
    if before is None and after:
        return after, "new file mode 100644"
    if after is None and before:
        # A plain diff gives no mode, so git refuses to delete a link or a
        # submodule with this line ("wrong type"): that fails safe.
        return before, "deleted file mode 100644"
    if before and before == after:
        return before, ""
    return None


def _with_git_headers(patch: str) -> str:
    """Give each file of a plain unified diff the ``diff --git`` line that the path check needs.

    Models often write ``--- a/X``/``+++ b/X`` with no ``diff --git`` line, and
    the SKY-353 check refuses any header outside a ``diff --git`` section.
    Hunk lines are counted, so a hunk line that looks like a header gets none.

    Args:
        patch: A diff that ends with a newline.

    Returns:
        The diff with a ``diff --git`` line (and a mode line for a new or
        deleted file) before each header pair; unchanged when it already has a
        ``diff --git`` line, any pair names no single ``a/``/``b/`` path or has
        no hunk right after it, or a hunk line is broken.
    """
    if re.search(r"(?m)^diff --git ", patch):
        return patch
    lines = patch.split("\n")
    out: list[str] = []
    old = new = 0
    for index, line in enumerate(lines):
        if old > 0 or new > 0:
            tag = line[:1] or " "
            if tag not in " -+\\":
                return patch
            old -= tag in " -"
            new -= tag in " +"
        elif line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            header = _plain_header_path(line[4:], lines[index + 1][4:])
            # A mode line after the pair would follow the inserted one, and git
            # takes the last: ``new file mode 120000`` would then make a link.
            if header is None or not _HUNK_HEADER.match(lines[index + 2]):
                return patch
            path, mode = header
            out.extend([f"diff --git a/{path} b/{path}", *([mode] if mode else [])])
        # A header with more digits than this regex takes is not a hunk here,
        # so the repair may mark lines git reads as hunk lines: git then calls
        # the patch corrupt, and the checker sees every inserted path.
        elif hunk := _HUNK_HEADER.match(line):
            old = 1 if hunk.group(1) is None else int(hunk.group(1))
            new = 1 if hunk.group(2) is None else int(hunk.group(2))
        out.append(line)
    return "\n".join(out)


def _unescape_backticks(text: str) -> str:
    """Turn each backtick token back into its run of backticks, and each ``<<<<`` back into ``<<``.

    Args:
        text: A repository sample as upstream parsed it.

    Returns:
        The sample as a real diff.
    """
    return _UNESCAPE.sub(lambda match: "<<" if match.group(1) is None else "`" * int(match.group(1)), text)


def _visible(seed: RepoSeed, path: str) -> bool:
    """Return whether a normalized path is neither hidden nor inside a hidden folder.

    Args:
        seed: The run's repository seed, with its hidden-path hashes.
        path: The normalized repository-relative path.

    Returns:
        ``True`` when the brief may name the path.
    """
    return hashed_kind(seed.hidden_hashes, path) is None


def _validation_finalists(task: Task, server: EvalServer) -> int:
    """Return how many top samples to re-score on the validation cases.

    Args:
        task: Train cases and the validation split.
        server: Run accounting.

    Returns:
        The number of finalists whose validation scoring the budget can
        reserve while leaving room for at least two full train samples, or 0
        when the task has no validation split.
    """
    if not task.valset:
        return 0
    per_sample = max(1, len(task.cases))
    spare = server.remaining - 2 * per_sample
    return max(0, min(_MAX_FINALISTS, spare // len(task.valset)))


def _select_on_validation(
    task: Task, server: EvalServer, sampling: EvalServer, adapted: Result, finalists: int
) -> Result:
    """Pick the winner among the best train samples by their validation mean.

    Args:
        task: Train cases and the validation split.
        server: Run accounting that still holds the reserved validation runs.
        sampling: The lane upstream sampled in; its records are train-only.
        adapted: Upstream's train-selected incumbent.
        finalists: How many top train samples to validate.

    Returns:
        The incumbent with the highest validation mean, scored as that mean.
        A sample with a metric error on any case has no mean, so it is never
        a finalist and never the winner.
    """
    per_sample = max(1, len(task.cases))
    complete = [record for record in sampling.history if record.count >= per_sample and record.mean_score is not None]
    ranked = sorted(complete, key=lambda record: record.mean_score, reverse=True)[:finalists]
    best: tuple[float, Any] | None = None
    for record in ranked:
        scores = [server.evaluate(record.candidate, case)[0] for case in task.valset]
        if any(score is None for score in scores):
            continue
        mean = sum(scores) / len(scores)
        if best is None or mean > best[0]:
            best = (mean, record.candidate)
    if best is None:
        return adapted
    adapted.best_candidate, adapted.best_score = best[1], best[0]
    adapted.total_evals = server.used
    adapted.metadata["selection_scope"] = "validation"
    return adapted
