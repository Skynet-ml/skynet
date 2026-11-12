"""An in-memory sandbox provider for tests.

It keeps files in a dict and runs a tiny command set (``true``, ``false``,
``exit N``, ``echo``, ``cat`` and ``printenv``), so the provider contract can
run without a network or a container.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import replace
from typing import ClassVar

from core.sandbox import (
    JOB_TAG,
    VERCEL_CAPABILITIES,
    EnvironmentCapabilities,
    ExecResult,
    NetworkPolicy,
    OutputSink,
    SandboxEnvironment,
    SandboxSpec,
)

FAKE_CAPABILITIES: EnvironmentCapabilities = replace(VERCEL_CAPABILITIES, name="fake", billable=False)


class InMemorySandbox(SandboxEnvironment):
    """Act as a sandbox provider with no external dependencies.

    ``script`` queues canned results for one command. ``fail_stop`` makes
    ``stop`` raise, so tests can check that a sweep continues.
    """

    capabilities: ClassVar[EnvironmentCapabilities] = FAKE_CAPABILITIES
    _running: ClassVar[set[InMemorySandbox]] = set()

    def __init__(self, spec: SandboxSpec) -> None:
        """Create a stopped fake sandbox.

        Args:
            spec: The sandbox to open.
        """
        super().__init__(spec)
        self.files: dict[str, str] = {}
        self.commands: list[str] = []
        self.applied_policies: list[NetworkPolicy] = []
        self.running = False
        self.start_count = 0
        self.fail_stop = False
        self._scripted: dict[str, list[ExecResult]] = {}

    def script(self, command: str, *results: ExecResult) -> None:
        """Queue results that the next runs of a command return in order.

        Args:
            command: The exact command string.
            *results: The results to return, one for each run.
        """
        self._scripted.setdefault(command, []).extend(results)

    async def start(self) -> None:
        """Mark the sandbox as running."""
        self.running = True
        self.start_count += 1
        type(self)._running.add(self)

    async def stop(self) -> None:
        """Mark the sandbox as stopped.

        Raises:
            RuntimeError: ``fail_stop`` is set.
        """
        if self.fail_stop:
            raise RuntimeError("fake stop failure")
        self.running = False
        type(self)._running.discard(self)

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> ExecResult:
        """Run one command from the small fake command set.

        Args:
            command: The shell command.
            cwd: Ignored.
            env: Extra variables that ``printenv`` can read.
            timeout_sec: Ignored.
            on_output: Receives the stdout and stderr text.
            sudo: Ignored.

        Returns:
            The scripted result, or the result of the fake command.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        self._require_running()
        self.commands.append(command)
        queued = self._scripted.get(command)
        result = queued.pop(0) if queued else self._run(command, {**self.spec.env, **(env or {})})
        if on_output is not None:
            if result.stdout:
                on_output("stdout", result.stdout)
            if result.stderr:
                on_output("stderr", result.stderr)
        return result

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Store text files in memory.

        Args:
            files: Maps each sandbox path to its text content.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        self._require_running()
        self.files.update(files)

    async def download_file(self, path: str) -> str | None:
        """Return a stored text file.

        Args:
            path: The sandbox path.

        Returns:
            The content, or ``None`` when the file does not exist.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        self._require_running()
        return self.files.get(path)

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every running fake sandbox with this job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandboxes that stopped.
        """
        stopped = 0
        for sandbox in list(cls._running):
            if sandbox.spec.tags.get(JOB_TAG) != job_id:
                continue
            try:
                await sandbox.stop()
            except RuntimeError:
                continue
            stopped += 1
        return stopped

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Record the new policy.

        Args:
            policy: The checked policy.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        self._require_running()
        self.applied_policies.append(policy)

    def _require_running(self) -> None:
        """Raise when the sandbox is not running.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if not self.running:
            raise RuntimeError("The fake sandbox is not running.")

    def _run(self, command: str, env: Mapping[str, str]) -> ExecResult:
        """Interpret one command from the fake command set.

        Args:
            command: The shell command.
            env: The environment that ``printenv`` reads.

        Returns:
            The command result; exit code 127 for an unknown command.
        """
        argv = shlex.split(command)
        if not argv:
            return ExecResult(0)
        name, args = argv[0], argv[1:]
        if name == "true":
            return ExecResult(0)
        if name == "false":
            return ExecResult(1)
        if name == "exit":
            return ExecResult(int(args[0]) if args else 0)
        if name == "echo":
            return ExecResult(0, stdout=" ".join(args) + "\n")
        if name == "cat":
            missing = [path for path in args if path not in self.files]
            if missing:
                return ExecResult(1, stderr=f"cat: {missing[0]}: No such file or directory\n")
            return ExecResult(0, stdout="".join(self.files[path] for path in args))
        if name == "printenv" and len(args) == 1:
            value = env.get(args[0])
            return ExecResult(1) if value is None else ExecResult(0, stdout=value + "\n")
        return ExecResult(127, stderr=f"{name}: command not found\n")
