"""The opt-in local test control server.

Five routes under ``/internal/test/v1``, bound to loopback, behind a token that
every one of them requires. The server validates a request into a
:class:`TestRunSpec`, hands it to the scheduler, and serves back run state and
an event stream. It owns transport, authentication, storage and fan-out, and
nothing else: it starts no process, writes no configuration, downloads nothing,
and never touches a service.

The order of work in a request matters as much as the checks themselves.
Authentication happens before routing, routing before body reading, body
reading before parsing, and parsing before the harness is reached, so a
rejected request is rejected without anything downstream being invoked.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol, Self, runtime_checkable
from urllib.parse import urlsplit

from ..test_contracts import TestHarness, TestProviderMode, TestRunSpec, TestRunState
from .auth import TestApiToken, presented_token
from .binding import LOOPBACK_HOST, resolve_bind_host
from .payloads import (
    CONTENT_TYPE,
    MAX_BODY_BYTES,
    PayloadError,
    event_payload,
    parse_run_spec,
    run_report_payload,
    run_spec_payload,
)
from .runs import (
    EventsAhead,
    EventsExpired,
    RunInProgress,
    RunRecord,
    RunStore,
    UnknownRun,
    is_run_id,
    utc_now,
)
from .sanitize import sanitize
from .scheduler import RunScheduler


class ShutdownTimeout(RuntimeError):
    """stop() could not finish because a run is still executing."""


API_PREFIX = "/internal/test/v1"
SERVER_NAME = "pepitos-ai-hub-test-control"
KEEPALIVE_COMMENT = b": keep-alive\n\n"


@runtime_checkable
class StatusSource(Protocol):
    """Read-only aggregate state the API republishes but does not gather."""

    def snapshot(self) -> Mapping[str, object]: ...


class EmptyStatusSource:
    """The default: the API reports only what it knows about itself."""

    def snapshot(self) -> Mapping[str, object]:
        return {}


@dataclass(frozen=True)
class TestApiConfig:
    """Everything the server needs, decided before it starts listening."""

    __test__ = False

    token: TestApiToken
    known_profiles: frozenset[str]
    host: str = LOOPBACK_HOST
    port: int = 0
    allow_real_providers: bool = False
    allow_network: bool = False
    allow_gpu: bool = False
    keepalive_seconds: float = 15.0
    started_at: str = field(default_factory=utc_now, compare=False)


class _Http(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    api: TestApi

    def handle_error(self, request: object, client_address: object) -> None:
        """A reader that walked away is not an error, and nothing is printed.

        The default would write a traceback to stderr, which is both noise and
        a way for request content to end up in a log. Anything that is not a
        dropped connection is recorded on the server so a test can see it.
        """
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionError | TimeoutError):
            return
        self.api.handler_errors.append(f"{type(error).__name__}: {error}")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_NAME
    sys_version = ""

    # --- plumbing -----------------------------------------------------------

    def log_message(self, format: str, *args: object) -> None:
        """Silence. A request line can carry a token someone put in a query."""

    def version_string(self) -> str:
        return SERVER_NAME

    @property
    def api(self) -> TestApi:
        return self.server.api  # type: ignore[attr-defined]

    def _send(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, slug: str, detail: str = "") -> None:
        payload: dict[str, object] = {"error": slug}
        if detail:
            payload["detail"] = detail
        if status is HTTPStatus.UNAUTHORIZED:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(status, payload)

    def _authenticated(self) -> bool:
        """Every route, health included. Nothing is parsed before this passes."""
        if self.api.config.token.matches(presented_token(self.headers)):
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "unauthorized")
        return False

    def _route(self) -> list[str]:
        """The path segments under the API prefix, with the query discarded."""
        path = urlsplit(self.path).path
        if not path.startswith(API_PREFIX):
            return []
        remainder = path[len(API_PREFIX) :].strip("/")
        return remainder.split("/") if remainder else []

    # --- methods ------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._authenticated():
            return
        route = self._route()
        if route == ["health"]:
            self._send(HTTPStatus.OK, self.api.health_payload())
            return
        if route == ["status"]:
            self._send(HTTPStatus.OK, self.api.status_payload())
            return
        if len(route) == 2 and route[0] == "runs":
            self._run(route[1])
            return
        if len(route) == 3 and route[0] == "runs" and route[2] == "events":
            self._events(route[1])
            return
        self._error(HTTPStatus.NOT_FOUND, "not-found")

    def do_POST(self) -> None:
        if not self._authenticated():
            return
        if self._route() != ["runs"]:
            self._error(HTTPStatus.NOT_FOUND, "not-found")
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            spec = parse_run_spec(payload, known_profiles=self.api.config.known_profiles)
        except PayloadError as error:
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", str(error))
            return
        refusal = self.api.gate(spec)
        if refusal:
            self._error(HTTPStatus.FORBIDDEN, "not-enabled", refusal)
            return
        try:
            record = self.api.scheduler.submit(spec)
        except RunInProgress as error:
            self._error(HTTPStatus.CONFLICT, "run-in-progress", str(error))
            return
        self._send(HTTPStatus.ACCEPTED, self.api.accepted_payload(record))

    def _unsupported(self) -> None:
        if not self._authenticated():
            return
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method-not-allowed")

    do_PUT = _unsupported
    do_PATCH = _unsupported
    do_DELETE = _unsupported
    do_HEAD = _unsupported
    do_OPTIONS = _unsupported

    # --- bodies -------------------------------------------------------------

    def _read_json(self) -> object | None:
        # A refused body is not drained, so the connection cannot be reused.
        self.close_connection = True
        if self.headers.get("Transfer-Encoding"):
            self._error(HTTPStatus.LENGTH_REQUIRED, "length-required")
            return None
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != CONTENT_TYPE:
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported-media-type")
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "length-required")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", "malformed Content-Length")
            return None
        if length < 0:
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", "malformed Content-Length")
            return None
        if length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body-too-large")
            return None
        try:
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", "the body is not valid JSON")
            return None
        self.close_connection = False
        return payload

    # --- runs ---------------------------------------------------------------

    def _run(self, run_id: str) -> None:
        if not is_run_id(run_id):
            self._error(HTTPStatus.NOT_FOUND, "not-found")
            return
        try:
            record = self.api.store.get(run_id)
        except UnknownRun:
            self._error(HTTPStatus.NOT_FOUND, "not-found")
            return
        self._send(HTTPStatus.OK, self.api.run_payload(record))

    def _events(self, run_id: str) -> None:
        if not is_run_id(run_id):
            self._error(HTTPStatus.NOT_FOUND, "not-found")
            return
        try:
            self.api.store.get(run_id)
        except UnknownRun:
            self._error(HTTPStatus.NOT_FOUND, "not-found")
            return

        raw_resume = self.headers.get("Last-Event-ID")
        after = 0
        if raw_resume is not None:
            try:
                after = int(raw_resume.strip())
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "invalid-request", "malformed Last-Event-ID")
                return
            if after < 0:
                self._error(HTTPStatus.BAD_REQUEST, "invalid-request", "malformed Last-Event-ID")
                return
        try:
            stream = self.api.store.follow(
                run_id,
                after=after,
                stopping=self.api.stopping,
                keepalive_seconds=self.api.config.keepalive_seconds,
            )
            first = next(stream, ...)
        except EventsAhead as error:
            self._error(HTTPStatus.CONFLICT, "events-ahead", str(error))
            return
        except EventsExpired as error:
            self._error(HTTPStatus.CONFLICT, "events-expired", str(error))
            return

        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            if first is not ...:
                self._write_event(first)
                for event in stream:
                    self._write_event(event)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _write_event(self, event) -> None:
        if event is None:
            self.wfile.write(KEEPALIVE_COMMENT)
            self.wfile.flush()
            return
        data = json.dumps(event_payload(event))
        frame = f"id: {event.sequence}\nevent: {event.kind.value}\ndata: {data}\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()


class TestApi:
    """The server object: configuration, storage, scheduling and lifetime."""

    __test__ = False

    def __init__(
        self,
        config: TestApiConfig,
        harness: TestHarness,
        *,
        status_source: StatusSource | None = None,
        store: RunStore | None = None,
    ) -> None:
        self.config = config
        self.harness = harness
        self.status_source = status_source or EmptyStatusSource()
        self.store = store or RunStore()
        self.scheduler = RunScheduler(harness, self.store)
        self.stopping = threading.Event()
        self.handler_errors: list[str] = []
        self._http: _Http | None = None
        self._thread: threading.Thread | None = None
        self._bound: tuple[str, int] | None = None

    # --- lifetime -----------------------------------------------------------

    def start(self) -> Self:
        """Bind and serve. Refuses any address that is not loopback."""
        if self._http is not None:
            raise RuntimeError("the test control server is already running")
        host = resolve_bind_host(self.config.host)
        http = _Http((host, self.config.port), _Handler)
        http.api = self
        self._http = http
        self._bound = http.server_address[:2]
        self._thread = threading.Thread(
            target=http.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="test-api-server",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop serving and release everything, or say that it could not.

        A run is never interrupted. The harness owns whatever it started, and
        cutting its thread down would leave a process, a file or a download
        half finished with nobody left to complete it.

        So the order is: refuse new work, stop accepting requests, then give
        the run the timeout to end on its own. If it is still going after that,
        this raises :class:`ShutdownTimeout` instead of closing the socket and
        reporting a shutdown that did not happen. Everything needed to finish
        is kept, so calling ``stop`` again once the run ends completes it.
        """
        self.stopping.set()
        if self._http is None:
            self.scheduler.shutdown(timeout=timeout)
            return
        self._http.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if not self.scheduler.shutdown(timeout=timeout):
            raise ShutdownTimeout(
                "the test control server stopped accepting requests, but a run is still "
                "executing and will not be interrupted; nothing was released. Call stop() "
                "again once the run has finished."
            )
        self._http.server_close()
        self._http = None
        self._thread = None
        self._bound = None

    def wait(self, timeout: float | None = None) -> None:
        """Block on the serving thread. Used by the command line entry point."""
        if self._thread is None:
            raise RuntimeError("the test control server is not running")
        self._thread.join(timeout=timeout)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exception: object) -> None:
        self.stop()

    @property
    def address(self) -> tuple[str, int]:
        if self._bound is None:
            raise RuntimeError("the test control server is not running")
        return self._bound

    @property
    def port(self) -> int:
        return self.address[1]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}{API_PREFIX}"

    # --- projections --------------------------------------------------------

    def gate(self, spec: TestRunSpec) -> str:
        """Why this run is not allowed here, or an empty string."""
        real = spec.provider_mode is TestProviderMode.LOCAL_REAL
        if real and not self.config.allow_real_providers:
            return "this server was not started with real providers enabled"
        if spec.allow_network and not self.config.allow_network:
            return "this server was not started with network access enabled"
        if spec.allow_gpu and not self.config.allow_gpu:
            return "this server was not started with GPU access enabled"
        return ""

    def health_payload(self) -> dict[str, object]:
        return {"status": "ok", "api": API_PREFIX, "started_at": self.config.started_at}

    def status_payload(self) -> dict[str, object]:
        """Aggregate state, sanitized, with the API's own runs alongside it."""
        active = self.store.active_run()
        snapshot = dict(self.status_source.snapshot())
        return {
            "server": {
                "state": "stopping" if self.stopping.is_set() else "running",
                "host": self._bound[0] if self._bound else self.config.host,
                "port": self._bound[1] if self._bound else self.config.port,
                "real_providers_enabled": self.config.allow_real_providers,
                "network_enabled": self.config.allow_network,
                "gpu_enabled": self.config.allow_gpu,
            },
            "runs": self.store.counts(),
            "active_run": self.run_payload(active) if active is not None else None,
            "system": sanitize(snapshot.get("system", {})),
            "gpu": sanitize(snapshot.get("gpu", {})),
            "services": sanitize(snapshot.get("services", {})),
            "models": sanitize(snapshot.get("models", {})),
        }

    def accepted_payload(self, record: RunRecord) -> dict[str, object]:
        # The state here is the one the run was accepted in. It may already
        # have moved on by the time this is read, which is what the run URL is
        # for; a 202 describes the acceptance.
        return {
            "run_id": record.run_id,
            "state": TestRunState.QUEUED.value,
            "created_at": record.created_at,
            "run": f"{API_PREFIX}/runs/{record.run_id}",
            "events": f"{API_PREFIX}/runs/{record.run_id}/events",
        }

    def run_payload(self, record: RunRecord) -> dict[str, object]:
        return {
            "run_id": record.run_id,
            "state": record.state.value,
            "spec": run_spec_payload(record.spec),
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "detail": record.detail,
            "report": run_report_payload(record.report) if record.report else None,
            "diagnostics": record.diagnostics(),
        }


__all__ = [
    "API_PREFIX",
    "EmptyStatusSource",
    "ShutdownTimeout",
    "StatusSource",
    "TestApi",
    "TestApiConfig",
]
