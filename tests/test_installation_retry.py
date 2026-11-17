"""Retry identity survives an unbound first install and conflict decisions."""

from __future__ import annotations

import threading
from collections import Counter

import httpx
import pytest
from first_run_support import CAPELLA, controlled_stack
from test_installation_switching import World, custom, failed, hub, install

from local_ai_stack.desktop.api import DesktopController, DesktopError
from local_ai_stack.download import Downloader
from local_ai_stack.e2e.seams import Injection, InjectionPlan
from local_ai_stack.external_conflict_contracts import (
    ConflictPolicySelection,
    ConflictResolutionStatus,
    ExternalConflictPolicy,
)
from local_ai_stack.hub_application import HubApplicationAdapter
from local_ai_stack.installation import InstallLocationMode, resolve_installation
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.stack_application import StackApplication
from local_ai_stack.tui.application import ConflictResolutionRequired, HubError, InstallSelection


def test_first_real_install_retries_recovered_source_at_exact_original_child(tmp_path):
    stack = controlled_stack()
    recovered = False
    interrupted = InjectionPlan(frozenset({Injection.DOWNLOAD_INTERRUPTED}))
    stack.app.deps.downloader = lambda: Downloader(
        client_factory=lambda: httpx.Client(
            transport=stack.artifacts.transport(None if recovered else interrupted)
        ),
        max_attempts=1,
        chunk_size=8,
    )
    profiles = tuple(stack.app.profiles.values())
    parent = tmp_path / "chosen-parent"
    child = parent / "Capella"
    adapter = HubApplicationAdapter(
        profiles=profiles,
        resolve=lambda location: resolve_installation(
            location,
            repo_root=stack.fixture.path,
            env={ENV_ROOT: str(tmp_path / "default")},
        ),
        backend_factory=lambda installation: StackApplication(
            installation,
            profiles=profiles,
            repo_root=stack.fixture.path,
            options=stack.app.options,
            dependencies=stack.app.deps,
        ),
        selection_path=tmp_path / "hub-state.yaml",
    )
    try:
        first = adapter.install(
            InstallSelection(CAPELLA, InstallLocationMode.CUSTOM, str(parent), True)
        )
        adapter.wait(20)
        assert adapter.current_operation().last_event.state is OperationState.FAILED
        assert adapter.config is None and adapter.active_root() is None
        assert stack.artifacts.interrupted
        partials = list(child.rglob("*.part"))
        assert partials and any(path.stat().st_size for path in partials)
        assert adapter.retryable_installation() == {
            "operation_id": first,
            "profile_id": CAPELLA,
            "root": str(child),
            "kind": "install",
        }
        requests = Counter(stack.artifacts.requests)
        components = [url for url in requests if stack.artifacts.kinds[url] == "component"]
        assert components

        # Even a resolver that now points somewhere else cannot move this retry.
        recovered = True
        adapter.resolve = lambda _location: pytest.fail(
            "retry must not resolve the selection again"
        )
        second = adapter.retry_installation(first)
        assert second != first
        adapter.wait(20)
        assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
        assert adapter.active_profile() == CAPELLA
        assert adapter.active_root() == child
        assert adapter.config.installation.mode is InstallLocationMode.CUSTOM
        assert not (child / "Capella").exists()
        assert adapter.retryable_installation() is None
        for url in components:
            assert stack.artifacts.requests.count(url) == requests[url]
        with pytest.raises(HubError):
            adapter.retry_installation(first)
    finally:
        adapter.close()
        stack.release()


@pytest.mark.parametrize("mode", [InstallLocationMode.DEFAULT, InstallLocationMode.PORTABLE])
def test_retry_preserves_noncustom_mode_and_previously_resolved_root(tmp_path, mode):
    world = World()
    adapter = hub(tmp_path, world)
    selection = InstallSelection("pepe-8gb", mode)
    target = adapter.resolve(selection.location("unused")).root
    try:
        world.failing.add(target)
        first = adapter.install(selection)
        adapter.wait(10)
        assert adapter.config is None and adapter.active_root() is None
        assert adapter.retryable_installation()["root"] == str(target)
        world.failing.remove(target)
        adapter.resolve = lambda _location: pytest.fail("retry must reuse its resolved root")
        adapter.retry_installation(first)
        adapter.wait(10)
        assert adapter.active_root() == target
        assert adapter.config.installation.mode is mode
        assert adapter.active_profile() == "pepe-8gb"
    finally:
        adapter.close()


def test_installation_config_write_failure_is_retryable_before_backend_started(
    tmp_path, monkeypatch
):
    from local_ai_stack import hub_application

    world = World()
    adapter = hub(tmp_path, world)
    root = tmp_path / "failed"
    write = hub_application.write_installed_config

    def refuse(*_args, **_kwargs):
        raise OSError("controlled disk refusal")

    try:
        monkeypatch.setattr(hub_application, "write_installed_config", refuse)
        first = adapter.install(custom(root, "pepe-8gb"))
        adapter.wait(10)
        assert adapter.current_operation().last_event.state is OperationState.FAILED
        assert adapter.retryable_installation()["operation_id"] == first
        assert adapter.config is None and adapter.active_root() is None
        assert world.calls == []
        monkeypatch.setattr(hub_application, "write_installed_config", write)
        adapter.retry_installation(first)
        adapter.wait(10)
        assert adapter.active_root() == root
        assert adapter.active_profile() == "pepe-8gb"
    finally:
        adapter.close()


@pytest.mark.parametrize("previous_active", [False, True])
def test_retry_keeps_original_nondefault_profile_and_previous_binding(tmp_path, previous_active):
    world = World()
    adapter = hub(tmp_path, world)
    old = tmp_path / "old"
    # Use the shipped display name rather than assuming its filesystem spelling.
    profile = next(profile for profile in adapter.profiles if profile.id == "pepe-8gb")
    target = tmp_path / "parent" / profile.name
    try:
        if previous_active:
            install(adapter, custom(old))
        original = adapter.config
        world.failing.add(target)
        first = adapter.install(
            InstallSelection("pepe-8gb", InstallLocationMode.CUSTOM, str(target.parent), True)
        )
        adapter.wait(10)
        if original is None:
            assert adapter.config is None
        else:
            assert adapter.config.profile_id == original.profile_id
            assert adapter.config.installation == original.installation
        assert adapter.active_root() == (old if previous_active else None)
        assert adapter.retryable_installation()["profile_id"] == "pepe-8gb"

        world.failing.remove(target)
        second = adapter.retry_installation(first)
        adapter.wait(10)
        assert second != first
        assert adapter.active_root() == target
        assert adapter.active_profile() == "pepe-8gb"
        assert world.calls[-1] == (target, "first_run", "pepe-8gb")
        assert adapter.retryable_installation() is None
    finally:
        adapter.close()


def test_retry_preflight_retains_failed_id_for_existing_conflict_resolution(tmp_path):
    world = World()
    adapter = hub(tmp_path, world)
    root = tmp_path / "failed"
    try:
        world.failing.add(root)
        first = adapter.install(custom(root, "pepe-8gb"))
        adapter.wait(10)
        world.conflicting.add(root)
        with pytest.raises(ConflictResolutionRequired) as conflict:
            adapter.retry_installation(first)
        assert adapter.retryable_installation()["operation_id"] == first
        report = adapter.current_conflict_preflight()
        assert report is conflict.value.report
        assert adapter.active_root() is None
        assert world.commands_on(root) == ["first_run"]

        result = adapter.resolve_external_conflicts(
            ConflictPolicySelection(
                report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
            )
        )
        assert result.status is ConflictResolutionStatus.PROCEED
        world.conflicting.remove(root)
        world.failing.remove(root)
        second = adapter.retry_installation(first)
        adapter.wait(10)
        assert second != first and adapter.active_root() == root
        assert world.calls[-1] == (root, "first_run", "pepe-8gb")
    finally:
        adapter.close()


def test_old_retry_id_cannot_control_newer_attempt_and_unrelated_work_invalidates_retry(tmp_path):
    world = World()
    adapter = hub(tmp_path, world)
    root = tmp_path / "failed"
    entered, release = threading.Event(), threading.Event()
    try:
        world.failing.add(root)
        first = adapter.install(custom(root))
        adapter.wait(10)
        backend = world.backends[root]
        run = backend.first_run

        def blocked(operation_id, profile_id, *, emit=None):
            entered.set()
            assert release.wait(10)
            return run(operation_id, profile_id, emit=emit)

        backend.first_run = blocked
        second = adapter.retry_installation(first)
        assert entered.wait(10)
        assert adapter.retryable_installation() is None
        for action in ("pause", "resume", "cancel"):
            with pytest.raises(HubError):
                adapter.control_installation(action, first)
        with pytest.raises(HubError):
            adapter.retry_installation(first)
        assert adapter.installation_control()["operation_id"] == second
        release.set()
        adapter.wait(10)
        assert adapter.retryable_installation()["operation_id"] == second
        with pytest.raises(HubError):
            adapter.retry_installation(first)

        adapter.stop_stack()
        adapter.wait(10)
        assert adapter.retryable_installation() is None
        with pytest.raises(HubError):
            adapter.retry_installation(second)
    finally:
        release.set()
        adapter.close()


def test_failed_repair_retries_original_backend_even_after_switch(tmp_path):
    world = World()
    adapter = hub(tmp_path, world)
    first, other = tmp_path / "first", tmp_path / "other"
    try:
        install(adapter, custom(first, "pepe-8gb"))
        install(adapter, custom(other))
        adapter.switch_installation(first)
        backend = world.backends[first]
        repair = backend.repair
        backend.repair = lambda operation_id, profile_id, **_kwargs: failed(operation_id, "offline")
        failed_id = adapter.repair()
        adapter.wait(10)
        assert adapter.retryable_installation() == {
            "operation_id": failed_id,
            "profile_id": "pepe-8gb",
            "root": str(first),
            "kind": "repair",
        }
        adapter.switch_installation(other)
        backend.repair = repair
        retry_id = adapter.retry_installation(failed_id)
        adapter.wait(10)
        assert retry_id != failed_id
        assert world.calls[-1] == (first, "repair", "pepe-8gb")
        assert adapter.active_root() == other and adapter.active_profile() == "pepe"
        assert adapter.retryable_installation() is None
    finally:
        adapter.close()


def test_desktop_projects_and_requires_failed_operation_identity(tmp_path):
    world = World()
    app = hub(tmp_path, world)
    controller = DesktopController(app, folder_picker=object())
    root = tmp_path / "failed"
    try:
        world.failing.add(root)
        first = app.install(custom(root, "pepe-8gb"))
        app.wait(10)
        assert controller.state()["retryable_installation"] == {
            "operation_id": first,
            "profile_id": "pepe-8gb",
            "root": str(root),
            "kind": "install",
        }
        for args in ({}, {"operation_id": "stale"}, {"operation_id": ""}):
            with pytest.raises(DesktopError):
                controller.command("retry_installation", args)
        world.failing.remove(root)
        reply = controller.command("retry_installation", {"operation_id": first})
        app.wait(10)
        assert set(reply) == {"operation_id"} and reply["operation_id"] != first
        assert app.active_root() == root
        assert controller.state()["retryable_installation"] is None
    finally:
        controller.close()
