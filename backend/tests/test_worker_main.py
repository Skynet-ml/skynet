"""Boot test for the standalone worker entrypoint."""

from __future__ import annotations

import threading
import types
from unittest.mock import MagicMock, create_autospec

import worker_main
from core.storage.remote import RemoteDBJobStore


def test_run_worker_boots_against_the_real_store_signatures(monkeypatch):
    """Start and stop the worker with a store whose methods keep their real signatures."""
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
    ):
        monkeypatch.setattr(worker_main, name, MagicMock())
    monkeypatch.setattr(worker_main, "get_job_store", lambda: store)
    monkeypatch.setattr(worker_main, "signal", types.SimpleNamespace(signal=MagicMock(), SIGTERM=15, SIGINT=2))
    monkeypatch.setattr(worker_main, "threading", types.SimpleNamespace(Event=lambda: stopped))
    monkeypatch.setattr(worker_main.gc, "freeze", lambda: None)

    worker_main.run_worker()

    store.recover_orphaned_jobs.assert_called_once_with()
    worker_main.get_worker.return_value.stop.assert_called_once_with()
