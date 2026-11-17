"""Loopback-only static assets and authenticated, bounded JSON API."""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..hub_application import HubApplicationAdapter
from ..resources import resource_root
from ..tui.application import HubError
from .api import DesktopController, DesktopError


def default_static_root() -> Path:
    return resource_root() / "desktop-ui" / "dist"


MAX_BODY_BYTES = 16 * 1024


class DesktopServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self, app: HubApplicationAdapter | DesktopController, static_root: Path | None = None
    ) -> None:
        """Serve a real Hub, or a configured controller for its injected OS openers.

        Bind errors close the controller this constructor created.
        """
        self.static_root = Path(static_root or default_static_root()).resolve()
        if not (self.static_root / "index.html").is_file():
            raise RuntimeError(
                "Interface desktop ausente. Execute npm ci e npm run build em "
                "desktop-ui, ou reinstale o pacote completo do Hub."
            )
        self.controller = app if isinstance(app, DesktopController) else DesktopController(app)
        self.token = secrets.token_urlsafe(32)
        try:
            super().__init__(("127.0.0.1", 0), DesktopRequestHandler)
        except Exception:
            self.controller.close()
            raise
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"
        self.url = f"{self.origin}/#token={self.token}"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.controller.start_observing()
        self._thread = threading.Thread(target=self.serve_forever, name="desktop-http", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop HTTP, then wait for Hub work and close it. Services keep running."""
        try:
            if self._thread is not None:
                self.shutdown()
                self._thread.join()
            self.server_close()
        finally:
            self.controller.close()


class DesktopRequestHandler(BaseHTTPRequestHandler):
    server: DesktopServer
    server_version = "PepitosHub"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args) -> None:
        # URLs, request headers and credentials never go to logs/stdout.
        pass

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, payload: object) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, error: DesktopError) -> None:
        self._json(error.status, error.payload())

    def _authorize(self, *, api: bool) -> None:
        if self.headers.get_all("Host") != [self.server.authority]:
            raise DesktopError("Host local inválido.", "forbidden_host", 403)
        origins = self.headers.get_all("Origin")
        if origins is not None and origins != [self.server.origin]:
            raise DesktopError("Origem não autorizada.", "forbidden_origin", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise DesktopError("Solicitação externa não autorizada.", "forbidden_origin", 403)
        if api:
            headers = self.headers.get_all("Authorization") or []
            expected = f"Bearer {self.server.token}"
            if len(headers) != 1 or not hmac.compare_digest(headers[0].encode(), expected.encode()):
                raise DesktopError(
                    "Sessão não autorizada. Abra novamente o Hub.", "unauthorized", 401
                )

    def _handle(self) -> None:
        try:
            parsed = urlsplit(self.path)
            api = parsed.path.startswith("/api/")
            # Static pages may carry renderer state (?page=); the API never takes a query.
            if parsed.scheme or parsed.netloc or parsed.fragment or (api and parsed.query):
                raise DesktopError("Endereço de solicitação inválido.")
            self._authorize(api=api)
            if parsed.path == "/api/state" and self.command == "GET":
                self._json(200, self.server.controller.state())
                return
            if parsed.path == "/api/command" and self.command == "POST":
                if self.headers.get_content_type() != "application/json":
                    raise DesktopError(
                        "Envie JSON com Content-Type application/json.", "unsupported_media", 415
                    )
                lengths = self.headers.get_all("Content-Length") or []
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise DesktopError("O tamanho do corpo é obrigatório.", "invalid_length", 400)
                try:
                    length = int(lengths[0])
                except ValueError:
                    raise DesktopError("Tamanho do corpo inválido.") from None
                if length < 0 or length > MAX_BODY_BYTES:
                    raise DesktopError(
                        "A solicitação excede o limite permitido.", "payload_too_large", 413
                    )
                try:
                    payload = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise DesktopError("O corpo não contém JSON válido.") from None
                if not isinstance(payload, dict) or set(payload) != {"command", "args"}:
                    raise DesktopError("Envie apenas command e args.")
                result = self.server.controller.command(payload["command"], payload["args"])
                self._json(200, {"ok": True, "result": result})
                return
            if api:
                raise DesktopError("Rota ou método não disponível.", "not_found", 404)
            if self.command not in {"GET", "HEAD"}:
                raise DesktopError("Método não permitido.", "method_not_allowed", 405)
            decoded = unquote(parsed.path)
            if "\\" in decoded or "\x00" in decoded or ".." in Path(decoded).parts:
                raise DesktopError("Caminho de arquivo não permitido.", "unsafe_path", 403)
            relative = decoded.lstrip("/") or "index.html"
            target = (self.server.static_root / relative).resolve()
            if not target.is_relative_to(self.server.static_root):
                raise DesktopError("Caminho de arquivo não permitido.", "unsafe_path", 403)
            if not target.is_file() and not Path(relative).suffix:
                # Client-side routes reload the app shell; missing assets still 404.
                target = self.server.static_root / "index.html"
            if not target.is_file():
                raise DesktopError("Arquivo não encontrado.", "not_found", 404)
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".js":
                content_type = "text/javascript"
            self._send(200, target.read_bytes(), content_type)
        except DesktopError as exc:
            self._error(exc)
        except HubError as exc:
            self._error(DesktopError(str(exc), "unavailable", 503))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:  # noqa: BLE001 -- the renderer gets JSON, never a traceback
            self._error(
                DesktopError(
                    "Não foi possível concluir a solicitação. Tente novamente; "
                    "consulte os diagnósticos se o problema persistir.",
                    "internal_error",
                    500,
                )
            )

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_OPTIONS = _handle
