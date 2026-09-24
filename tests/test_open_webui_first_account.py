"""Open WebUI's first account is made after the Hub integrated it; the Hub catches up.

A new installation starts Open WebUI before anyone has an account in it, and
every sync (image filter, chat model record, image edit settings) goes through
that first administrator. The integration at start therefore finds no
account and changes nothing; the person then makes the account in the browser
and chats without ever coming back to the Hub. Measured on a real 8 GB
installation: no filter, so an edit request made a new picture; no model
record, so the 3B model answered with raw tool-call JSON.

Against the real composition: the real adapter, ``StackApplication`` and
first-run coordinator; the Open WebUI syncs and its account database are the
controlled seams, recorded here.
"""

from __future__ import annotations

import threading

from test_delivery_wave_a import composition  # noqa: F401 -- the real composition fixture

from local_ai_stack.open_webui_filters import FilterSync, FilterSyncReport
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.tui.application import InstallSelection


class OpenWebUIAccounts:
    """Open WebUI's account table and the three syncs that need its administrator."""

    def __init__(self) -> None:
        self.admin: str | None = None
        self.synced: list[str] = []
        self.refused: list[str] = []
        self.done = threading.Event()

    def first_admin(self, _data_dir) -> str | None:
        return self.admin

    def _sync(self, subject: str) -> FilterSyncReport:
        if self.admin is None:
            self.refused.append(subject)
            return FilterSyncReport(FilterSync.NO_ADMIN, f"{subject}: no administrator yet")
        self.synced.append(subject)
        self.done.set()
        return FilterSyncReport(FilterSync.INSTALLED, f"{subject}: installed")

    def bind(self, deps) -> None:
        deps.first_admin = self.first_admin
        deps.filter_sync = lambda *_args, **_kwargs: self._sync("filter")
        deps.model_sync = lambda *_args, **_kwargs: self._sync("model")
        deps.image_edit_sync = lambda *_args, **_kwargs: self._sync("image-edit")


def installed_before_the_first_account(composition):  # noqa: F811 -- the fixture, passed in
    world, adapter, _ = composition()
    accounts = OpenWebUIAccounts()
    accounts.bind(world.app.deps)
    adapter.integration_poll_seconds = 0.01
    adapter.install(InstallSelection("capella"))
    adapter.wait(30)
    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
    assert adapter.backend.status("capella").stack_up
    assert accounts.refused and not accounts.synced, "integrated before any account existed"
    assert adapter.backend.open_webui_integration_waiting()
    return world, adapter, accounts


def test_the_first_account_made_after_the_integration_is_synced_without_the_hub(composition):  # noqa: F811
    world, adapter, accounts = installed_before_the_first_account(composition)
    starts, stops = list(world.launcher.starts), list(world.launcher.stops)

    accounts.admin = "first-admin"  # the person signs up in the browser

    assert accounts.done.wait(10), "the Hub never synced once the account existed"
    adapter.wait(10)
    assert "filter" in accounts.synced, "the image filter reaches the running Open WebUI"
    assert not adapter.backend.open_webui_integration_waiting()
    assert world.launcher.starts == starts and world.launcher.stops == stops, (
        "catching up starts and stops nothing"
    )
    # The Hub is free again: an operation right after is not refused.
    adapter.stop_stack()
    adapter.wait(30)
    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED


def test_without_an_account_nothing_is_synced_and_operations_are_not_held(composition):  # noqa: F811
    _, adapter, accounts = installed_before_the_first_account(composition)
    refused = len(accounts.refused)

    # Many looks later, still no account: nothing is attempted and the Hub is free.
    for _ in range(3):
        adapter.restart_stack()
        adapter.wait(30)
        assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
    assert not accounts.synced
    assert adapter.backend.open_webui_integration_waiting()
    assert len(accounts.refused) > refused, "each start still tries, and still finds none"


def test_a_stopped_open_webui_is_not_waited_on(composition):  # noqa: F811
    _, adapter, accounts = installed_before_the_first_account(composition)

    adapter.stop_stack()
    adapter.wait(30)

    assert not adapter.backend.open_webui_integration_waiting(), "nothing left to sync into"
    accounts.admin = "first-admin"
    assert not accounts.done.wait(0.3), "a stopped Open WebUI is left for its next start"
    adapter.start_stack()
    adapter.wait(30)
    assert "filter" in accounts.synced, "which integrates, now that the account exists"


def test_closing_the_hub_ends_the_watch(composition):  # noqa: F811
    _, adapter, _ = installed_before_the_first_account(composition)
    watch = adapter._integration_watch
    assert watch is not None and watch.is_alive()

    adapter.close()

    watch.join(5)
    assert not watch.is_alive()
