"""The effect log: each outside effect of Skynet's own, keyed by step id, runs once (SKY-712).

:meth:`EffectStore.once` first waits on :meth:`StepLogStore.durable`, so no
effect leaves Skynet for a step that a crash can still lose. Then it inserts
the ``agent_effects`` row and keeps that transaction open while the effect
runs, and commits the row with the result after it. A second caller of the
same key, in this worker or another, waits on that row until the first
commits, then returns the stored result.

A crash between the effect and its commit rolls the row back, so the next
call runs the effect again. The log is at-least-once across that crash: each
effect must find its own earlier result outside (an existing branch or pull
request) instead of doing it twice. User commands are not effects and stay
at-least-once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import AgentEffectModel
from .step_log_store import StepLogStore

# Only the open transaction of the running call holds a row in this state, so no other caller reads it.
RUNNING = "running"
DONE = "done"


class EffectStore:
    """Run each keyed outside effect of a run once, after its step is durable."""

    def __init__(self, engine: Engine, steps: StepLogStore) -> None:
        """Bind the store.

        Args:
            engine: Engine whose schema carries ``jobs`` and ``agent_effects``.
            steps: The step log whose ``durable`` barrier each effect waits on.
        """
        self._engine = engine
        self._steps = steps

    def once(
        self,
        run_id: str,
        effect_key: str,
        fn: Callable[[], dict[str, Any]],
        *,
        step_id: str,
        execution_generation: int,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait until the step is durable, then run ``fn`` once for the key and store its result.

        Args:
            run_id: The job the effect belongs to.
            effect_key: The idempotency key, for example ``<step_id>:push``.
            fn: The effect; it returns a JSON object.
            step_id: The step that asks for the effect.
            execution_generation: The generation the step was committed under.
            timeout: The most seconds to wait for the step to be durable.

        Returns:
            The result of ``fn``, or the stored result of an earlier call with the same key.

        Raises:
            StepFenceError: When the step failed the fence; ``fn`` does not run.
            TimeoutError: When the step is not durable in time; ``fn`` does not run.
        """
        self._steps.durable(step_id, execution_generation, timeout)
        insert = postgres_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            # On a key that another open transaction inserted, this insert waits for that commit or rollback.
            claimed = session.execute(
                insert(AgentEffectModel)
                .values(
                    run_id=run_id,
                    effect_key=effect_key,
                    step_id=step_id,
                    state=RUNNING,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing()
            ).rowcount
            if not claimed:
                return dict(session.get(AgentEffectModel, (run_id, effect_key)).result)
            result = fn()
            session.execute(
                update(AgentEffectModel)
                .where(AgentEffectModel.run_id == run_id, AgentEffectModel.effect_key == effect_key)
                .values(state=DONE, result=result, updated_at=datetime.now(UTC))
            )
            session.commit()
        return result


@dataclass(frozen=True)
class StepEffects:
    """The effect log bound to one step: each effect name of the step runs once."""

    store: EffectStore
    run_id: str
    step_id: str
    execution_generation: int

    def once(self, name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run one named effect of the step once, keyed ``<step_id>:<name>``.

        Args:
            name: The effect's name within the step, for example ``push``.
            fn: The effect; it returns a JSON object.

        Returns:
            The result of ``fn``, or the stored result of an earlier call.
        """
        return self.store.once(
            self.run_id,
            f"{self.step_id}:{name}",
            fn,
            step_id=self.step_id,
            execution_generation=self.execution_generation,
        )
