"""Boot test for the standalone worker entrypoint."""

from __future__ import annotations

import threading
import types
from unittest.mock import MagicMock, create_autospec

import pytest

import worker_main
from core.storage.remote import RemoteDBJobStore


@pytest.mark.parametrize("compactor_started", [True, False])
def test_run_worker_boots_against_the_real_store_signatures(monkeypatch, compactor_started):
    """Start and stop the worker; stop the tree compactor it gets, or stop cleanly when it gets ``None``."""
    store = create_autospec(RemoteDBJobStore, instance=True)
    store.recover_pending_jobs.return_value = []
    stopped = threading.Event()
    stopped.set()
    for name in (
        "configure_logging",
        "configure_error_reporting",
        "configure_notification_preferences",
        "configure_data_policy",
        "get_worker",
        "start_queue_metrics_refresher",
        "start_orphan_recovery_sweeper",
        "start_embedding_index_sweeper",
        "start_tree_compactor",
    ):
        monkeypatch.setattr(worker_main, name, MagicMock())
    compactor = MagicMock()
    worker_main.start_tree_compactor.return_value = compactor if compactor_started else None
    monkeypatch.setattr(worker_main, "get_job_store", lambda: store)
    monkeypatch.setattr(worker_main, "signal", types.SimpleNamespace(signal=MagicMock(), SIGTERM=15, SIGINT=2))
    monkeypatch.setattr(worker_main, "threading", types.SimpleNamespace(Event=lambda: stopped))
    monkeypatch.setattr(worker_main.gc, "freeze", lambda: None)

    worker_main.run_worker()

    store.recover_orphaned_jobs.assert_called_once_with()
    worker_main.get_worker.return_value.stop.assert_called_once_with()
    worker_main.start_tree_compactor.assert_called_once_with(store.engine)
    # With None, the run itself is the check: a None in the sweeper list fails at its stop() call.
    if compactor_started:
        compactor.stop.assert_called_once_with()
