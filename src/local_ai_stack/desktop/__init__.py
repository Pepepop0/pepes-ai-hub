"""Desktop entry point: native WebView2, or an explicit browser development mode."""

from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

from .. import PRODUCT_NAME
from ..hub_application import create_hub_application
from ..resources import resource_root
from .api import DesktopController
from .instance_lock import InstanceLock
from .server import DesktopServer
from .window_state import (
    MIN_HEIGHT,
    MIN_WIDTH,
    WindowGeometry,
    focus_existing_window,
    load_geometry,
    save_geometry,
    virtual_screen,
)

_MB_OK, _MB_YESNOCANCEL = 0x0, 0x3
_IDYES, _IDNO = 6, 7
_MB_ICONERROR, _MB_ICONQUESTION, _MB_ICONWARNING = 0x10, 0x20, 0x30
_MB_TOPMOST_FOREGROUND = 0x40000 | 0x10000
# How long closing with "stop services" waits for the stop before giving up.
STOP_ON_CLOSE_SECONDS = 120.0


def _message_box(message: str, flags: int) -> int | None:
    """Synchronous Win32 dialog; safe from pywebview's GUI-thread closing callback."""
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.windll.user32.MessageBoxW(
        None, message, PRODUCT_NAME, flags | _MB_TOPMOST_FOREGROUND
    )


def _startup_error(message: str, *, browser: bool, flags: int = _MB_ICONERROR) -> None:
    """Browser mode has a terminal; the windowed app has only a native dialog."""
    if (browser or _message_box(message, _MB_OK | flags) is None) and sys.stderr is not None:
        print(message, file=sys.stderr)


ALREADY_OPEN = (
    "O Hub já está aberto em outra janela, ou um comando do Hub está alterando as "
    "instalações agora. Use a janela aberta ou aguarde o comando terminar."
)
CLOSE_WITH_SERVICES = (
    "Os serviços desta instalação estão em execução.\n\n"
    "Sim: parar os serviços e fechar o Hub.\n"
    "Não: fechar o Hub e manter os serviços em execução.\n"
    "Cancelar: continuar no Hub."
)


def _current_geometry(window) -> WindowGeometry | None:
    """Where the window is now; its restored bounds when maximized. None when unreadable."""
    try:
        form = getattr(window, "native", None)
        if form is None:
            return WindowGeometry(window.width, window.height, window.x, window.y)
        scale = getattr(form, "_scale", 1) or 1
        state = str(form.WindowState)
        bounds = form.Bounds if state == "Normal" else form.RestoreBounds
        return WindowGeometry(
            max(MIN_WIDTH, int(bounds.Width / scale)),
            max(MIN_HEIGHT, int(bounds.Height / scale)),
            int(bounds.X / scale),
            int(bounds.Y / scale),
            state == "Maximized",
        )
    except Exception:  # noqa: BLE001 -- geometry is a convenience, never a reason to fail closing
        return None


def run_desktop(
    *, browser: bool = False, open_browser: bool = True, repo_root: Path | None = None
) -> int:
    """Run until the window closes; never stop managed services implicitly.

    Browser mode prints its capability URL intentionally. Native mode never prints it.
    The process holds the single-writer lock for its whole life; a second one exits 1.
    """
    hub = None
    server = None
    lock = None
    try:
        lock = InstanceLock()
        if not lock.acquire():
            lock = None
            # A second launch is usually someone looking for the window they already have.
            if not browser and focus_existing_window(PRODUCT_NAME):
                return 0
            _startup_error(ALREADY_OPEN, browser=browser, flags=_MB_ICONWARNING)
            return 1
        root = Path(repo_root) if repo_root is not None else resource_root()
        hub = create_hub_application(root)
        server = DesktopServer(hub, root / "desktop-ui" / "dist")
        controller = server.controller
        server.start()
        if browser:
            print(server.url, flush=True)
            if open_browser:
                webbrowser.open(server.url)
            print(
                "Pressione Ctrl+C para encerrar o Hub; os serviços continuam em execução.",
                flush=True,
            )
            idle = threading.Event()
            try:
                # Short waits keep Ctrl+C responsive on Windows.
                while not idle.wait(0.5):
                    pass
            except KeyboardInterrupt:
                # The finally block waits for operations and leaves services running.
                pass
            return 0
        try:
            import webview
        except ImportError as exc:
            raise RuntimeError(
                "A interface nativa requer pywebview. Reinstale o Hub com as "
                "dependências desktop; no Windows, instale também WebView2 Runtime."
            ) from exc
        geometry = load_geometry(screen=virtual_screen())
        window = webview.create_window(
            PRODUCT_NAME,
            server.url,
            width=geometry.width,
            height=geometry.height,
            x=geometry.x,
            y=geometry.y,
            maximized=geometry.maximized,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            background_color="#171719",
            text_select=True,
        )
        leaving = threading.Event()

        def stop_then_close() -> None:
            try:
                controller.command("stop", {})
                controller.app.wait(STOP_ON_CLOSE_SECONDS)
                snapshot = controller.app.current_operation()
                state = getattr(getattr(snapshot, "state", None), "value", None)
                if state != "succeeded":
                    raise RuntimeError(getattr(snapshot, "message", "") or "A parada falhou.")
            except TimeoutError:
                detail = "Os serviços ainda estão parando. Aguarde e tente fechar novamente."
            except Exception as exc:  # noqa: BLE001 -- shown to the person, window stays open
                detail = str(exc)
            else:
                leaving.set()
                window.destroy()
                return
            _message_box(
                f"Não foi possível parar os serviços. O Hub continua aberto.\n\n{detail}",
                _MB_OK | _MB_ICONWARNING,
            )

        def closing() -> bool:
            if not leaving.is_set():
                if controller.busy:
                    _message_box(
                        "Aguarde a operação atual terminar antes de fechar o Hub. "
                        "Fechar agora poderia interromper o trabalho em andamento.",
                        _MB_OK | _MB_ICONWARNING,
                    )
                    return False
                if controller.managed_services_running():
                    answer = _message_box(CLOSE_WITH_SERVICES, _MB_YESNOCANCEL | _MB_ICONQUESTION)
                    if answer == _IDYES:
                        threading.Thread(
                            target=stop_then_close, name="desktop-stop-on-close", daemon=True
                        ).start()
                        return False
                    if answer is not None and answer != _IDNO:
                        return False
            # A request may have started while the confirmation was on screen.
            if not controller.prepare_close():
                return False
            current = _current_geometry(window)
            if current is not None:
                save_geometry(current)
            return True

        window.events.closing += closing
        options = {"debug": False, "private_mode": True}
        if sys.platform == "win32":
            options["gui"] = "edgechromium"
        icon = root / "assets" / "app.ico"
        if icon.is_file():
            options["icon"] = str(icon)
        webview.start(**options)
        return 0
    except Exception as exc:  # noqa: BLE001 -- startup failures must reach a native dialog
        _startup_error(f"Não foi possível abrir o Hub. {exc}", browser=browser)
        return 1
    finally:
        try:
            if server is not None:
                server.close()
            elif hub is not None:
                hub.close()
        finally:
            if lock is not None:
                lock.release()


__all__ = ["DesktopController", "DesktopServer", "run_desktop"]
