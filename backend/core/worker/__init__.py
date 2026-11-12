"""Background worker for repository optimization jobs.

Engine, log handler, and subprocess runner are reached via their
submodules (``core.worker.engine``, ``core.worker.log_handler``,
``core.worker.subprocess_runner``). Re-exporting from ``__init__``
would force every importer of any submodule to load ``engine`` and,
through it, the whole ``service_gateway`` package. Modules that
``engine`` loads may import a worker submodule back, which would make a
cycle. Keeping this file empty prevents it.
"""

from __future__ import annotations

__all__: list[str] = []
