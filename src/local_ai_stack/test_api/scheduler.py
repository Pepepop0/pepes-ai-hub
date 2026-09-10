"""Handing a validated run to the harness and collecting what it produced.

This is the whole of the API's relationship with the harness: one call to
:meth:`TestHarness.run` on a worker thread, with a sink that stores whatever the
harness emits. The API decides when a run may start and what happens to the
events; it decides nothing about what a phase does.

Two guarantees are added on this side, and only these two. A run always reaches
a final state, including when the harness raises rather than returns. And a run
produces exactly one terminal event: the harness's own if it emitted one, and
otherwise one built from the report it returned, so a stream always ends.
"""

from __future__ import annotations

import threading
import time

from ..test_contracts import (
    TestEvent,
    TestEventKind,
    TestHarness,
    TestRunReport,
    TestRunSpec,
    TestRunState,
)
from .payloads import run_report_payload
from .runs import RunRecord, RunStore, failed_report, report_state, utc_now
from .sanitize import sanitize_text


class RunScheduler:
    """Runs one test run at a time, on a thread the server owns."""

    def __init__(self, harness: TestHarness, store: RunStore) -> None:
        self._harness = harness
        self._store = store
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def submit(self, spec: TestRunSpec) -> RunRecord:
        """Queue a run and start it. Refuses while another run is active."""
        record = self._store.create(spec)
        worker = threading.Thread(
            target=self._execute,
            args=(record.run_id, spec),
            name=f"test-api-run-{record.run_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            self._threads.append(worker)
        worker.start()
        return record

    def _execute(self, run_id: str, spec: TestRunSpec) -> None:
        self._store.mark_running(run_id)

        def emit(event: TestEvent) -> None:
            self._store.append(run_id, event)

        try:
            report = self._harness.run(run_id, spec, emit=emit)
        except Exception as error:  # noqa: BLE001 - a harness failure is a run result
            detail = sanitize_text(f"{type(error).__name__}: {error}")
            self._conclude(run_id, spec, failed_report(run_id, spec, detail), TestRunState.FAILED)
            return

        if not isinstance(report, TestRunReport):
            detail = "the harness returned something that is not a run report"
            self._conclude(run_id, spec, failed_report(run_id, spec, detail), TestRunState.FAILED)
            return

        self._conclude(run_id, spec, report, report_state(report))

    def _conclude(
        self,
        run_id: str,
        spec: TestRunSpec,
        report: TestRunReport,
        state: TestRunState,
    ) -> None:
        record = self._store.get(run_id)
        if not record.terminal_emitted:
            # The harness ended without a terminal event. The stream still has
            # to end, so one is written from the report rather than invented.
            self._store.append(
                run_id,
                TestEvent(
                    run_id=run_id,
                    sequence=1,
                    kind=TestEventKind.RESULT,
                    timestamp=utc_now(),
                    message=report.detail,
                    data={"report": run_report_payload(report)},
                ),
            )
        self._store.finish(run_id, state=state, report=report, detail=report.detail)

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Wait for in-flight runs, and report whether any is still going.

        A run is never interrupted. The harness owns whatever it started, so
        the only honest options are to wait for it or to say it is still there.
        The deadline is shared across workers, so the wait is bounded by
        ``timeout`` rather than by ``timeout`` per thread.
        """
        with self._lock:
            threads = list(self._threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            return not self._threads

    @property
    def busy(self) -> bool:
        with self._lock:
            return any(thread.is_alive() for thread in self._threads)


__all__ = ["RunScheduler"]
