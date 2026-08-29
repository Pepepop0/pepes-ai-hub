from __future__ import annotations

import socket

import psutil
import pytest

from local_ai_stack import inventory
from local_ai_stack.inventory import (
    Listener,
    ListenerReport,
    ScanOutcome,
    host_matches,
    list_listeners,
    list_processes,
)


@pytest.fixture
def listening_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


@pytest.mark.parametrize(
    ("bound", "wanted", "expected"),
    [
        ("127.0.0.1", "127.0.0.1", True),
        ("0.0.0.0", "127.0.0.1", True),
        ("::", "127.0.0.1", True),
        ("::1", "127.0.0.1", True),
        ("192.168.0.10", "127.0.0.1", False),
    ],
)
def test_host_matches(bound, wanted, expected):
    assert host_matches(bound, wanted) is expected


def test_list_listeners_sees_a_real_socket(listening_port):
    report = list_listeners()
    assert report.usable
    assert report.on_port(listening_port)


def test_on_port_ignores_other_ports(listening_port):
    report = list_listeners()
    assert report.on_port(listening_port + 1 if listening_port < 65535 else 1) == ()


def test_list_listeners_without_psutil(monkeypatch):
    monkeypatch.setattr(inventory, "psutil", None)
    report = list_listeners()
    assert report.outcome is ScanOutcome.UNAVAILABLE
    assert not report.usable
    assert report.on_port(11434) == ()
    assert "psutil" in report.detail


def test_list_listeners_when_access_is_denied(monkeypatch):
    def deny(*args, **kwargs):
        raise psutil.AccessDenied(pid=None, name="net_connections")

    monkeypatch.setattr(inventory.psutil, "net_connections", deny)
    report = list_listeners()
    assert report.outcome is ScanOutcome.UNAVAILABLE
    assert "AccessDenied" in report.detail


def test_list_processes_returns_this_machine_processes():
    report = list_processes()
    assert report.usable
    assert report.processes
    assert all(isinstance(p.pid, int) for p in report.processes)


def test_list_processes_excludes_the_calling_process():
    import os

    report = list_processes()
    assert report.by_pid(os.getpid()) is None
    assert list_processes(exclude_self=False).by_pid(os.getpid()) is not None


def test_list_processes_without_psutil(monkeypatch):
    monkeypatch.setattr(inventory, "psutil", None)
    report = list_processes()
    assert report.outcome is ScanOutcome.UNAVAILABLE
    assert report.processes == ()


def test_list_processes_when_the_scan_fails(monkeypatch):
    def deny(*args, **kwargs):
        raise psutil.AccessDenied(pid=None, name="process_iter")

    monkeypatch.setattr(inventory.psutil, "process_iter", deny)
    report = list_processes()
    assert report.outcome is ScanOutcome.UNAVAILABLE
    assert "AccessDenied" in report.detail


def test_empty_report_is_a_trustworthy_no():
    report = ListenerReport(ScanOutcome.OK, (Listener("127.0.0.1", 8080),))
    assert report.usable
    assert report.on_port(8080)
    assert report.on_port(11434) == ()
