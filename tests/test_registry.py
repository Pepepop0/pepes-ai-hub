from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_ai_stack import registry as registry_module
from local_ai_stack.inventory import (
    LookupOutcome,
    ProcessInfo,
    ProcessLookup,
    ProcessReport,
    ScanOutcome,
)
from local_ai_stack.layout import StackLayout
from local_ai_stack.registry import (
    REGISTRY_SCHEMA_VERSION,
    EntryStatus,
    IdentityVerdict,
    ManagedProcess,
    ManagedProcessRegistry,
    build_entry,
)

ENTRY = ManagedProcess(
    service_id="ollama",
    pid=4242,
    executable=r"C:\apps\ollama\ollama.exe",
    argv=(r"C:\apps\ollama\ollama.exe", "serve"),
    started_at=1000.0,
    create_time=500.0,
    log_file=r"C:\logs\ollama.log",
)

LIVE = ProcessInfo(4242, "ollama.exe", r"C:\apps\ollama\ollama.exe", "ollama.exe serve", 500.0)
IMPOSTOR = ProcessInfo(4242, "chrome.exe", r"C:\chrome\chrome.exe", "chrome.exe --tab", 9000.0)


def make_registry(tmp_path: Path) -> ManagedProcessRegistry:
    return ManagedProcessRegistry(tmp_path / "state" / "managed-processes.json", clock=lambda: 7.0)


def patch_lookup(monkeypatch, lookup: ProcessLookup) -> None:
    monkeypatch.setattr(registry_module, "lookup_process", lambda pid: lookup)


def report(*processes: ProcessInfo) -> ProcessReport:
    return ProcessReport(ScanOutcome.OK, processes)


# --- persistence -----------------------------------------------------------


def test_registry_round_trips_an_entry(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.load().entries == {}
    registry.register(ENTRY)
    reloaded = registry.load()
    assert reloaded.ok
    assert reloaded.get("ollama") == ENTRY


def test_registry_writes_a_documented_schema(tmp_path):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    document = json.loads(registry.path.read_text(encoding="utf-8"))
    assert document["schema_version"] == REGISTRY_SCHEMA_VERSION
    assert document["updated_at"] == 7.0
    assert document["processes"][0]["pid"] == 4242
    assert document["processes"][0]["argv"] == list(ENTRY.argv)


def test_save_is_atomic_and_leaves_no_temporary_file(tmp_path):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    registry.register(ManagedProcess("comfyui", 11, "comfy.exe"))
    leftovers = [p.name for p in registry.path.parent.iterdir() if p != registry.path]
    assert leftovers == []
    assert set(registry.load().entries) == {"ollama", "comfyui"}


def test_failed_save_does_not_destroy_the_previous_file(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    original = registry.path.read_text(encoding="utf-8")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registry_module.os, "replace", explode)
    with pytest.raises(OSError):
        registry.register(ManagedProcess("comfyui", 11, "comfy.exe"))
    assert registry.path.read_text(encoding="utf-8") == original
    assert [p for p in registry.path.parent.iterdir() if p != registry.path] == []


def test_dry_run_never_writes(tmp_path):
    registry = make_registry(tmp_path)
    registry.register(ENTRY, dry_run=True)
    assert not registry.path.exists()
    registry.register(ENTRY)
    registry.unregister("ollama", dry_run=True)
    assert registry.load().get("ollama") == ENTRY


def test_unregister_removes_only_the_named_service(tmp_path):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    registry.register(ManagedProcess("comfyui", 11, "comfy.exe"))
    assert registry.unregister("ollama") is True
    assert registry.unregister("ollama") is False
    assert set(registry.load().entries) == {"comfyui"}


def test_registry_uses_the_stack_layout_state_directory(tmp_path):
    layout = StackLayout.from_root(tmp_path / "root")
    registry = ManagedProcessRegistry.from_layout(layout)
    assert registry.path == layout.state_file("managed-processes")
    registry.register(ENTRY)
    assert registry.path.is_file()


# --- malformed state -------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json at all", "unreadable registry"),
        ("[]", "not an object"),
        ('{"schema_version": 99, "processes": []}', "unsupported registry schema_version"),
        ('{"schema_version": 1, "processes": {}}', "not a list"),
    ],
)
def test_malformed_state_is_tolerated_and_explained(tmp_path, content, message):
    registry = make_registry(tmp_path)
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text(content, encoding="utf-8")
    snapshot = registry.load()
    assert snapshot.entries == {}
    assert not snapshot.ok
    assert message in snapshot.error


def test_malformed_entries_are_skipped_but_good_ones_survive(tmp_path):
    registry = make_registry(tmp_path)
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "processes": [
                    ENTRY.to_dict(),
                    {"service_id": "broken"},
                    {"pid": 5, "executable": "x.exe"},
                    {"service_id": "neg", "pid": -1, "executable": "x.exe"},
                    "garbage",
                ],
            }
        ),
        encoding="utf-8",
    )
    snapshot = registry.load()
    assert set(snapshot.entries) == {"ollama"}
    assert "ignored 4 malformed" in snapshot.error


# --- identity --------------------------------------------------------------


def test_creation_time_proves_identity():
    assert ENTRY.identity_matches(LIVE)
    assert ENTRY.identity_matches(ProcessInfo(4242, "ollama.exe", None, "", 500.4))


def test_recycled_pid_is_not_our_process():
    assert not ENTRY.identity_matches(IMPOSTOR)
    assert not ENTRY.identity_matches(ProcessInfo(4242, "ollama.exe", None, "", 900.0))


def test_a_different_pid_never_matches():
    assert not ENTRY.identity_matches(ProcessInfo(1, "ollama.exe", None, "", 500.0))


def test_identity_falls_back_to_executable_when_creation_time_is_missing():
    entry = ManagedProcess("ollama", 4242, r"C:\apps\ollama\ollama.exe", ("ollama.exe", "serve"))
    assert entry.identity_matches(ProcessInfo(4242, "ollama.exe", None, "ollama.exe serve"))
    assert not entry.identity_matches(ProcessInfo(4242, "chrome.exe", r"C:\c\chrome.exe", "chrome"))


def test_fingerprint_is_readable():
    assert ENTRY.fingerprint == "ollama.exe#4242"


# --- verification ----------------------------------------------------------


def test_live_entry_is_actionable(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    patch_lookup(monkeypatch, ProcessLookup(LookupOutcome.FOUND, LIVE))
    status = registry.status("ollama")
    assert status.verdict is IdentityVerdict.LIVE
    assert status.live and status.actionable
    assert registry.managed_pids() == frozenset({4242})


def test_dead_pid_is_stale(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    patch_lookup(monkeypatch, ProcessLookup(LookupOutcome.NOT_FOUND, detail="no process"))
    status = registry.status("ollama")
    assert status.verdict is IdentityVerdict.STALE
    assert not status.actionable
    assert registry.managed_pids() == frozenset()


def test_recycled_pid_is_mismatch_and_never_actionable(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    patch_lookup(monkeypatch, ProcessLookup(LookupOutcome.FOUND, IMPOSTOR))
    status = registry.status("ollama")
    assert status.verdict is IdentityVerdict.MISMATCH
    assert not status.actionable
    assert "chrome.exe" in status.detail
    assert registry.managed_pids() == frozenset()


def test_uninspectable_pid_is_unknown_and_never_actionable(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    patch_lookup(monkeypatch, ProcessLookup(LookupOutcome.UNAVAILABLE, detail="psutil missing"))
    status = registry.status("ollama")
    assert status.verdict is IdentityVerdict.UNKNOWN
    assert not status.actionable
    assert registry.managed_pids() == frozenset()


def test_verification_can_reuse_one_process_scan(tmp_path):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    assert registry.status("ollama", processes=report(LIVE)).verdict is IdentityVerdict.LIVE
    assert registry.status("ollama", processes=report()).verdict is IdentityVerdict.STALE
    assert registry.status("ollama", processes=report(IMPOSTOR)).verdict is IdentityVerdict.MISMATCH


def test_unusable_process_report_falls_back_to_a_direct_lookup(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    patch_lookup(monkeypatch, ProcessLookup(LookupOutcome.FOUND, LIVE))
    unusable = ProcessReport(ScanOutcome.UNAVAILABLE, detail="denied")
    assert registry.status("ollama", processes=unusable).verdict is IdentityVerdict.LIVE


def test_status_of_an_unregistered_service_is_none(tmp_path):
    assert make_registry(tmp_path).status("ghost") is None


# --- pruning ---------------------------------------------------------------


def test_prune_drops_dead_and_recycled_entries_only(tmp_path):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    registry.register(ManagedProcess("comfyui", 11, "comfy.exe", create_time=1.0))
    registry.register(ManagedProcess("open-webui", 12, "python.exe", create_time=2.0))
    live_comfy = ProcessInfo(11, "comfy.exe", "comfy.exe", "comfy.exe", 1.0)
    stolen = ProcessInfo(12, "notepad.exe", "notepad.exe", "notepad", 555.0)

    class Scan(ManagedProcessRegistry):
        def verify(self, entry, *, processes=None):
            table = {11: live_comfy, 12: stolen}
            found = table.get(entry.pid)
            if found is None:
                return EntryStatus(entry, IdentityVerdict.STALE, detail="gone")
            return self._compare(entry, found)

    scanning = Scan(registry.path, clock=lambda: 7.0)
    assert sorted(scanning.prune()) == ["ollama", "open-webui"]
    assert set(scanning.load().entries) == {"comfyui"}


def test_prune_dry_run_keeps_the_file(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.register(ENTRY)
    patch_lookup(monkeypatch, ProcessLookup(LookupOutcome.NOT_FOUND))
    assert registry.prune(dry_run=True) == ("ollama",)
    assert set(registry.load().entries) == {"ollama"}


# --- entry construction ----------------------------------------------------


def test_build_entry_captures_the_live_creation_time_of_this_process():
    entry = build_entry("fixture", os.getpid(), "python.exe", ["python.exe", "-c", "pass"])
    assert entry.pid == os.getpid()
    assert entry.create_time is not None
    assert entry.argv == ("python.exe", "-c", "pass")
    assert entry.started_at > 0


def test_build_entry_survives_a_pid_that_does_not_exist(monkeypatch):
    patch_lookup(monkeypatch, ProcessLookup(LookupOutcome.NOT_FOUND))
    entry = build_entry("fixture", 999999, Path("x.exe"), clock=lambda: 3.0)
    assert entry.create_time is None
    assert entry.started_at == 3.0
