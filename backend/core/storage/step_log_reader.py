"""Read-only pages of a run's agent step log, for the replay viewer (SKY-726).

:class:`~core.storage.step_log_store.StepLogStore` is the writer: it owns a
flusher thread and a buffer, and its shard lookup records a shard on the
first use of a run. The API must not write, so this reader reads the run's
recorded shard itself and reads only flushed rows and their pack bytes.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .base import ObjectStore
from .models import AgentStepModel, JobModel
from .shard_map import MAIN, ShardMap, UnroutableRunError

# A step body above this size is not read at all: one page reads at most ``limit`` bodies.
BODY_READ_MAX = 256 * 1024
# Each text field of a step is cut to this many characters, so one huge output cannot fill the response.
FIELD_MAX = 16 * 1024


def _run_engine(main: Engine, shards: ShardMap, run_id: str) -> Engine:
    """Return the engine of the shard that the run records, with no write.

    Args:
        main: The main database, which holds ``jobs``.
        shards: The step log shard map.
        run_id: The job id.

    Returns:
        The shard engine; ``main`` when the run records no shard yet.

    Raises:
        UnroutableRunError: When the run records a shard that this map does not have.
    """
    with Session(main) as session:
        name = session.scalar(select(JobModel.step_log_shard).where(JobModel.optimization_id == run_id)) or MAIN
    if name not in shards.names():
        raise UnroutableRunError(f"run {run_id} records step log shard {name!r}, which this pod does not have")
    return shards.engine(name)


def _text(value: Any) -> str | None:
    """Return a body value as text: strings as they are, other JSON values as indented JSON.

    Args:
        value: One value of a step body.

    Returns:
        The text, or ``None`` for a missing value.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


def _cap(value: str | None) -> tuple[str | None, bool]:
    """Cut a text field to ``FIELD_MAX`` characters.

    Args:
        value: The text, or ``None``.

    Returns:
        The kept text and whether it was cut.
    """
    if value is None or len(value) <= FIELD_MAX:
        return value, False
    return value[:FIELD_MAX], True


def _fields(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """Pick the command, output and model response out of a step body.

    The step body has no fixed schema: each writer stores its own keys, so
    the reader takes the common ones and shows a model body that has no
    ``response`` key as JSON.

    Args:
        kind: ``command`` or ``model``.
        body: The step body.

    Returns:
        ``command``, ``output``, ``response`` (each capped) and ``truncated``.
    """
    output = _text(body.get("output"))
    if output is None:
        streams = [body[name] for name in ("stdout", "stderr") if isinstance(body.get(name), str) and body[name]]
        output = "\n".join(streams) if streams else None
    response = _text(body.get("response"))
    if response is None and kind == "model":
        response = _text(body)
    command, cut_command = _cap(_text(body.get("command")))
    output, cut_output = _cap(output)
    response, cut_response = _cap(response)
    return {
        "command": command,
        "output": output,
        "response": response,
        "truncated": cut_command or cut_output or cut_response,
    }


def read_step_page(
    main: Engine,
    shards: ShardMap,
    objects: ObjectStore | None,
    run_id: str,
    *,
    agent_id: str | None,
    after_seq: int,
    limit: int,
) -> dict[str, Any]:
    """Return one page of one agent's flushed steps, oldest first, and the run's agents.

    Args:
        main: The main database.
        shards: The step log shard map.
        objects: The pack store; ``None`` when this pod has none, and then no body is read.
        run_id: The job id.
        agent_id: The agent to page; ``None`` picks the first agent by id.
        after_seq: Return only steps whose ``seq`` is above this one; ``-1`` for the start.
        limit: The most steps to return.

    Returns:
        ``agents`` (``agent_id`` and ``step_count`` each), ``agent_id``,
        ``steps``, ``has_more`` and ``next_after_seq``.

    Raises:
        UnroutableRunError: When the run records a shard that this map does not have.
    """
    engine = _run_engine(main, shards, run_id)
    with Session(engine) as session:
        agents = [
            {"agent_id": name, "step_count": count}
            for name, count in session.execute(
                select(AgentStepModel.agent_id, func.count())
                .where(AgentStepModel.run_id == run_id)
                .group_by(AgentStepModel.agent_id)
                .order_by(AgentStepModel.agent_id)
            )
        ]
        if agent_id is None and agents:
            agent_id = agents[0]["agent_id"]
        rows = (
            session.scalars(
                select(AgentStepModel)
                .where(
                    AgentStepModel.run_id == run_id,
                    AgentStepModel.agent_id == agent_id,
                    AgentStepModel.seq > after_seq,
                )
                .order_by(AgentStepModel.seq)
                .limit(limit + 1)
            ).all()
            if agent_id is not None
            else []
        )
        records = [
            {
                "step_id": row.step_id,
                "seq": row.seq,
                "kind": row.kind,
                "tree_id": row.tree_id,
                "created_at": row.created_at,
                "location": (row.pack_key, row.pack_offset, row.pack_length),
            }
            for row in rows
        ]
    page = records[:limit]
    steps = []
    for record in page:
        key, offset, length = record.pop("location")
        body = None
        if objects is not None and length <= BODY_READ_MAX:
            try:
                body = json.loads(objects.get(key, offset, length))
            except KeyError:
                # The compactor can retire a pack between the row read and the body read; the step still shows.
                body = None
        if isinstance(body, dict):
            fields = _fields(record["kind"], body)
        else:
            fields = {"command": None, "output": None, "response": None, "truncated": length > BODY_READ_MAX}
        steps.append({**record, **fields})
    return {
        "agents": agents,
        "agent_id": agent_id,
        "steps": steps,
        "has_more": len(records) > limit,
        "next_after_seq": page[-1]["seq"] if page else after_seq,
    }
