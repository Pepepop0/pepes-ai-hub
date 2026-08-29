from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from local_ai_stack import probes
from local_ai_stack.probes import ProbeOutcome, http_probe, tcp_probe


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            body = json.dumps({"version": "0.0.1"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b""
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        """Silence the default stderr access log."""


@pytest.fixture
def http_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def listening_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture
def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_tcp_probe_reports_open_port(listening_port):
    result = tcp_probe("127.0.0.1", listening_port, timeout=1.0)
    assert result.outcome is ProbeOutcome.OK
    assert result.ok
    assert result.latency_ms is not None


def test_tcp_probe_reports_refused_connection(monkeypatch):
    def raise_refused(*args, **kwargs):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(probes.socket, "create_connection", raise_refused)
    result = tcp_probe("127.0.0.1", 11434, timeout=1.0)
    assert result.outcome is ProbeOutcome.REFUSED
    assert result.detail == "connection refused"


def test_tcp_probe_on_a_real_closed_port_is_refused_or_timeout(closed_port):
    # Hosts differ: a closed loopback port answers RST (refused) on some
    # machines and is silently dropped by a local firewall on others.
    result = tcp_probe("127.0.0.1", closed_port, timeout=0.35)
    assert result.outcome in {ProbeOutcome.REFUSED, ProbeOutcome.TIMEOUT}


def test_tcp_probe_reports_timeout(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(probes.socket, "create_connection", raise_timeout)
    result = tcp_probe("127.0.0.1", 11434, timeout=0.05)
    assert result.outcome is ProbeOutcome.TIMEOUT
    assert "timeout" in result.detail


def test_tcp_probe_reports_unexpected_socket_error(monkeypatch):
    def raise_oserror(*args, **kwargs):
        raise OSError("network is unreachable")

    monkeypatch.setattr(probes.socket, "create_connection", raise_oserror)
    result = tcp_probe("127.0.0.1", 11434, timeout=0.05)
    assert result.outcome is ProbeOutcome.ERROR


def test_http_probe_returns_body_and_status(http_server):
    result = http_probe(f"{http_server}/ok", timeout=2.0)
    assert result.outcome is ProbeOutcome.OK
    assert result.status_code == 200
    assert json.loads(result.body) == {"version": "0.0.1"}


def test_http_probe_treats_404_as_a_successful_round_trip(http_server):
    result = http_probe(f"{http_server}/nope", timeout=2.0)
    assert result.outcome is ProbeOutcome.OK
    assert result.status_code == 404


def test_http_probe_unwraps_a_refused_connection(monkeypatch):
    def raise_connect_error(*args, **kwargs):
        try:
            raise ConnectionRefusedError(61, "Connection refused")
        except ConnectionRefusedError as exc:
            raise httpx.ConnectError("All connection attempts failed") from exc

    monkeypatch.setattr(probes.httpx, "get", raise_connect_error)
    result = http_probe("http://127.0.0.1:8080/health", timeout=1.0)
    assert result.outcome is ProbeOutcome.REFUSED


def test_http_probe_detects_refusal_from_the_message_alone(monkeypatch):
    def raise_connect_error(*args, **kwargs):
        raise httpx.ConnectError(
            "[WinError 10061] No connection could be made because the target "
            "machine actively refused it"
        )

    monkeypatch.setattr(probes.httpx, "get", raise_connect_error)
    result = http_probe("http://127.0.0.1:8080/health", timeout=1.0)
    assert result.outcome is ProbeOutcome.REFUSED


def test_http_probe_on_a_real_closed_port_never_reports_ok(closed_port):
    result = http_probe(f"http://127.0.0.1:{closed_port}/health", timeout=0.35)
    assert result.outcome in {ProbeOutcome.REFUSED, ProbeOutcome.TIMEOUT}


def test_http_probe_reports_timeout(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise httpx.ConnectTimeout("too slow")

    monkeypatch.setattr(probes.httpx, "get", raise_timeout)
    result = http_probe("http://127.0.0.1:8080/health", timeout=0.05)
    assert result.outcome is ProbeOutcome.TIMEOUT


def test_http_probe_reports_transport_error(monkeypatch):
    def raise_read_error(*args, **kwargs):
        raise httpx.ReadError("broken pipe")

    monkeypatch.setattr(probes.httpx, "get", raise_read_error)
    result = http_probe("http://127.0.0.1:8080/health", timeout=1.0)
    assert result.outcome is ProbeOutcome.ERROR
    assert "ReadError" in result.detail
