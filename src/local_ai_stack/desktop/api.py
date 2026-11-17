"""Explicit desktop capabilities over the existing Hub application.

No provisioning, process signalling, or renderer-selected Python dispatch lives here.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from .. import PRODUCT_NAME, __version__, civitai_auth, huggingface_auth, web_search_settings
from ..download_auth import download_auth_providers
from ..external_conflict_contracts import ConflictPolicySelection, ExternalConflictPolicy
from ..folder_picker import default_folder_picker
from ..hub_application import HubApplicationAdapter
from ..installation import InstallLocationMode
from ..installations import root_key
from ..layout import StackLayout
from ..models import FileModelSource
from ..prerequisites import (
    GIT_DOWNLOAD_URL,
    GitInstallResult,
    PrerequisiteInstallError,
    install_git_with_winget,
)
from ..tui.application import (
    ConflictResolutionRequired,
    HubError,
    HubUnavailable,
    InstallSelection,
    LaunchRefusal,
    OperationBusy,
    SwitchRequiresStop,
)


class DesktopError(Exception):
    def __init__(
        self, message: str, code: str = "invalid_request", status: int = 400, details: object = None
    ) -> None:
        super().__init__(message)
        self.code, self.status, self.details = code, status, details

    def payload(self) -> dict:
        """The error body every transport returns: HTTP and the agent CLI."""
        body = {"ok": False, "error": str(self), "code": self.code}
        if self.details is not None:
            body["details"] = self.details
        return body


def serialize(value: object) -> object:
    """Serialize only domain values, never arbitrary objects or their __dict__."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        result = {field.name: serialize(getattr(value, field.name)) for field in fields(value)}
        # These are domain projections, not transport inventions.
        for name in ("ok", "disposition", "available_policies", "recommended_policy"):
            if hasattr(type(value), name):
                result[name] = serialize(getattr(value, name))
        return result
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [serialize(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Unsupported desktop projection: {type(value).__name__}")


def open_directory(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# Required arguments, optional arguments. No arbitrary method names cross the boundary.
COMMANDS = {
    "plan": ({"profile_id", "mode"}, {"custom_root", "create_subfolder"}),
    "estimate": ({"profile_id", "mode"}, {"custom_root", "create_subfolder"}),
    "install": ({"profile_id", "mode"}, {"custom_root", "create_subfolder"}),
    "start": (set(), set()),
    "stop": (set(), set()),
    "restart": (set(), set()),
    "service_start": ({"service_id"}, set()),
    "service_stop": ({"service_id"}, set()),
    "service_restart": ({"service_id"}, set()),
    "open_chat": (set(), set()),
    "verify": (set(), set()),
    "repair": (set(), set()),
    "retry_installation": ({"operation_id"}, set()),
    "doctor": (set(), set()),
    "download_auth": ({"profile_id"}, set()),
    "hf_status": (set(), set()),
    "hf_logout": (set(), set()),
    "hf_login": ({"token"}, set()),
    "civitai_status": (set(), set()),
    "civitai_logout": (set(), set()),
    "civitai_login": ({"token"}, set()),
    "web_search_status": (set(), set()),
    "web_search_configure": ({"enabled"}, {"searxng_url"}),
    "brave_login": ({"token"}, set()),
    "brave_logout": (set(), set()),
    "image_search_login": ({"provider", "token"}, set()),
    "image_search_logout": ({"provider"}, set()),
    "install_git": ({"confirmation"}, set()),
    "choose_folder": (set(), {"initial_path"}),
    "plan_delete": ({"root"}, set()),
    "delete": ({"root", "confirmation"}, {"stop"}),
    "forget": ({"root"}, set()),
    "plan_switch": ({"root"}, set()),
    "switch": ({"root"}, {"stop"}),
    "register": ({"root"}, {"stop"}),
    "relocate": ({"old_root", "new_root"}, {"stop"}),
    "open_folder": ({"root", "kind"}, set()),
    "open_url": ({"url"}, set()),
    "resolve_conflicts": ({"preflight_id", "policy"}, set()),
    "cancel_pending": (set(), set()),
    "logs": ({"root"}, {"service", "lines"}),
    "measure": ({"root"}, set()),
    "service_targets": (set(), set()),
    "open_service": ({"service_id"}, set()),
    "choose_chat_model": ({"model_id"}, set()),
    "import_chat_model": ({"reference"}, set()),
    "remove_imported_chat_model": ({"model_id"}, set()),
    "pause_installation": ({"operation_id"}, set()),
    "resume_installation": ({"operation_id"}, set()),
    "cancel_installation": ({"operation_id"}, set()),
}

# Read-only or OS-presentation commands remain available while a Hub worker runs.
BUSY_SAFE_COMMANDS = frozenset(
    {
        "estimate",
        "plan_delete",
        "plan_switch",
        "open_folder",
        "open_url",
        "download_auth",
        "hf_status",
        "civitai_status",
        "web_search_status",
        "doctor",
        "choose_folder",
        "logs",
        "measure",
        "service_targets",
        "pause_installation",
        "resume_installation",
        "cancel_installation",
    }
)

# Reads of files the Hub does not write while they are read. They run outside the
# command lock, so a slow disk walk never makes every other button answer "busy".
UNLOCKED_COMMANDS = frozenset({"logs", "measure", "service_targets"})

# Commands that change nothing the Hub owns. The agent CLI runs them while the
# window holds the single-writer lock; every other command needs that lock.
READ_ONLY_COMMANDS = frozenset(
    {
        "plan",
        "estimate",
        "plan_delete",
        "plan_switch",
        "doctor",
        "download_auth",
        "hf_status",
        "civitai_status",
        "web_search_status",
        "logs",
        "measure",
        "service_targets",
    }
)

SERVICE_LABELS = {"ollama": "Ollama", "open-webui": "Open WebUI", "comfyui": "ComfyUI"}
# Services with a page a person opens in the browser.
OPENABLE_SERVICES = frozenset({"open-webui", "comfyui"})
LOG_LINES_DEFAULT, LOG_LINES_MAX = 200, 1000
# A tail never reads more than this from the end of a log.
LOG_TAIL_MAX_BYTES = 512 * 1024
# A size measurement stops here and says it is partial.
MEASURE_DEADLINE_SECONDS = 15.0
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def redirects(path: Path) -> bool:
    """A symlink or Windows junction: something that leads outside the folder it sits in.

    ``Path.is_junction`` only exists from Python 3.12; the attribute check works on 3.11.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _loopback_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme in {"http", "https"}
        and parts.hostname in {"localhost", "127.0.0.1", "::1"}
        and not parts.username
        and not parts.password
    )


# How often the observer probes the active backend while a renderer is reading
# state. Commands and operation events wake it early.
OBSERVE_INTERVAL_SECONDS = 1.0
# How long a state read waits for the observer's first answer.
FIRST_OBSERVATION_TIMEOUT_SECONDS = 60.0


class DesktopController:
    def __init__(
        self,
        app: HubApplicationAdapter,
        *,
        folder_opener: Callable[[Path], None] = open_directory,
        url_opener: Callable[[str], object] = webbrowser.open,
        folder_picker=None,
        git_installer: Callable[[], GitInstallResult] = install_git_with_winget,
    ) -> None:
        self.app = app
        self.folder_opener, self.url_opener = folder_opener, url_opener
        self.folder_picker = folder_picker or default_folder_picker()
        self.git_installer = git_installer
        self._commands = threading.Lock()
        self._events_lock = threading.Lock()
        # The service probe costs seconds (ports, model store, dry run). One
        # observer thread makes it; state reads return its latest answer.
        self._state_lock = threading.Lock()
        self._observed = threading.Condition()
        self._observation: tuple | None = None
        self._observer: threading.Thread | None = None
        self._refresh = threading.Event()
        self._stopping = threading.Event()
        self._events: deque = deque(maxlen=250)
        self._closed = False
        self._released = False
        self._subscription = app.subscribe(self._receive)

    def _observe_loop(self) -> None:
        while not self._stopping.is_set():
            config = self.app.config
            try:
                backend, _, status = self.app.runtime_status()
                observation = (backend, config, status, None)
            except Exception as exc:  # noqa: BLE001 -- surfaced to readers as unavailable
                observation = (None, config, None, exc)
            with self._observed:
                self._observation = observation
                self._observed.notify_all()
            self._refresh.wait(OBSERVE_INTERVAL_SECONDS)
            self._refresh.clear()

    def start_observing(self) -> None:
        """Begin probing now, so the first state read does not pay for the first probe."""
        with self._observed:
            if self._observer is None and not self._stopping.is_set():
                self._observer = threading.Thread(
                    target=self._observe_loop, name="desktop-observer", daemon=True
                )
                self._observer.start()

    def _active_status(self) -> object:
        """The latest probe of the active backend, made for the installation now in use."""
        self.start_observing()
        with self._observed:
            ready = self._observed.wait_for(
                lambda: self._observation is not None, FIRST_OBSERVATION_TIMEOUT_SECONDS
            )
            observation = self._observation
        unavailable = HubUnavailable("Não foi possível verificar a instalação. Tente novamente.")
        if not ready or observation is None:
            raise unavailable
        backend, config, status, error = observation
        if config is not self.app.config or (error is None and backend is not self.app.backend):
            # The installation changed after the probe: answer for the new one now.
            try:
                return self.app.runtime_status()[2]
            except Exception as exc:
                raise unavailable from exc
        if error is not None:
            raise unavailable from error
        return status

    def _receive(self, event) -> None:
        with self._events_lock:
            self._events.append(event)
        self._refresh.set()

    def operation_busy(self) -> bool:
        try:
            self.app.wait(0)
        except TimeoutError:
            return True
        return False

    @property
    def busy(self) -> bool:
        return self._commands.locked() or self.operation_busy()

    def prepare_close(self) -> bool:
        """Atomically refuse closing during commands or actual domain workers."""
        if not self._commands.acquire(blocking=False):
            return False
        try:
            if self.operation_busy():
                return False
            self._closed = True
            return True
        finally:
            self._commands.release()

    def close(self) -> None:
        """Idempotent; waits for the Hub worker instead of interrupting the installer."""
        with self._commands:
            self._closed = True
            if self._released:
                return
            self._released = True
            self._stopping.set()
            self._refresh.set()
            if self._observer is not None:
                self._observer.join()
            self._subscription.close()
            self.app.close()

    def models(self) -> list[dict]:
        rows = []
        for model_id, spec in self.app.catalog_specs().items():
            source = spec.source
            artifact = source.artifact if isinstance(source, FileModelSource) else None
            rows.append(
                {
                    "id": model_id,
                    "provider": spec.provider.value,
                    "kind": spec.kind.value,
                    "revision": spec.revision,
                    "profile_ids": [
                        profile.id
                        for profile in self.app.profiles
                        if any(str(req.model_id) == model_id for req in profile.model_requirements)
                    ],
                    "source_url": artifact.url if artifact else None,
                    "size_bytes": artifact.size_bytes if artifact else source.size_bytes,
                }
            )
        return rows

    def state(self) -> dict:
        with self._state_lock:
            return self._state()

    def _state(self) -> dict:
        status = self._active_status()
        installation, services, vram = self.app.projections(status)
        with self._events_lock:
            events = tuple(self._events)
        histories = {root_key(entry.root): entry for entry in self.app.known_installations()}
        installations = []
        for summary in self.app.installation_summaries(active_status=status):
            row = serialize(summary)
            entry = histories.get(root_key(summary.root))
            if entry is not None:
                row.update(
                    last_install=serialize(entry.last_install),
                    last_repair=serialize(entry.last_repair),
                    last_failure=serialize(entry.last_failure),
                )
            # What the last install or repair applied, and what this Hub ships
            # now: two numbers, so the screen can say when a repair updates.
            row.update(
                profile_version=entry.profile_version if entry is not None else None,
                hub_profile_version=self.app.profile_version(summary.profile_id),
            )
            installations.append(row)
        profiles = [
            {**serialize(choice), "version": self.app.profile_version(choice.id)}
            for choice in self.app.profile_choices()
        ]
        return serialize(
            {
                "product": PRODUCT_NAME,
                "version": __version__,
                "installation": installation,
                "installations": installations,
                "profiles": profiles,
                "services": services,
                "vram": vram,
                "operation": self.app.current_operation(),
                "installation_control": self.app.installation_control(),
                "retryable_installation": self.app.retryable_installation(),
                "events": events,
                "busy": self.busy,
                "conflict": self.app.current_conflict_preflight(),
                "models": self.models(),
                "chat": self._chat_models(),
                "settings": {
                    "recent_custom_roots": self.app.recent_custom_roots(),
                    "services_lifetime": "Ao fechar com serviços em execução, o Hub pergunta se "
                    "deve pará-los ou mantê-los rodando. Operações em andamento precisam "
                    "terminar antes de fechar.",
                },
            }
        )

    def _chat_models(self) -> dict | None:
        """The chat model choice of the installation in use; None when it cannot be read."""
        try:
            return self.app.chat_models()
        except Exception:  # noqa: BLE001 -- a missing view never fails the whole state
            return None

    @staticmethod
    def _text(args: dict, key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise DesktopError(f"Informe um valor válido para {key}.")
        return value

    def _path(self, args: dict, key: str = "root", *, known: bool = False) -> Path:
        value = self._text(args, key)
        path = Path(value)
        if not path.is_absolute() or value.startswith(("\\\\", "//")) or ".." in path.parts:
            raise DesktopError("Escolha uma pasta local com caminho absoluto, sem '..'.")
        if known and not any(
            root_key(item.root) == root_key(path) for item in self.app.known_installations()
        ):
            raise DesktopError(
                "Esta pasta não é uma instalação conhecida pelo Hub.", "unknown_installation", 403
            )
        return path

    def _selection(self, args: dict) -> InstallSelection:
        profile_id = self._text(args, "profile_id")
        choice = next((p for p in self.app.profile_choices() if p.id == profile_id), None)
        if choice is None:
            raise DesktopError("Este perfil não está disponível nesta versão do Hub.")
        mode = InstallLocationMode(self._text(args, "mode"))
        custom = args.get("custom_root")
        if mode is InstallLocationMode.CUSTOM:
            custom = str(self._path(args, "custom_root"))
        create_subfolder = args.get("create_subfolder", mode is InstallLocationMode.CUSTOM)
        if type(create_subfolder) is not bool:
            raise DesktopError("A opção de criar uma subpasta deve ser verdadeira ou falsa.")
        return InstallSelection(profile_id, mode, custom, create_subfolder)

    def command(self, command: str, args: dict) -> object:
        if not isinstance(command, str) or command not in COMMANDS or not isinstance(args, dict):
            raise DesktopError("Comando desconhecido ou argumentos inválidos.")
        required, optional = COMMANDS[command]
        if not required <= args.keys() or args.keys() - required - optional:
            raise DesktopError("Os argumentos não correspondem ao comando solicitado.")
        if "stop" in args and type(args["stop"]) is not bool:
            raise DesktopError("A autorização para parar serviços deve ser verdadeira ou falsa.")
        if command in UNLOCKED_COMMANDS:
            if self._closed:
                raise DesktopError("O Hub está encerrando.", "closing", 409)
            return self._run(command, args)
        if not self._commands.acquire(blocking=False):
            raise DesktopError("Aguarde o comando atual terminar.", "busy", 409)
        try:
            if self._closed:
                raise DesktopError("O Hub está encerrando.", "closing", 409)
            if command not in BUSY_SAFE_COMMANDS and self.operation_busy():
                raise DesktopError("Aguarde a operação atual terminar.", "busy", 409)
            return self._run(command, args)
        finally:
            self._commands.release()
            self._refresh.set()

    def _run(self, command: str, args: dict) -> object:
        """Execute one validated command; domain refusals become transport errors."""
        try:
            return serialize(self._execute(command, args))
        except ConflictResolutionRequired as exc:
            raise DesktopError(str(exc), "conflict_required", 409, serialize(exc.report)) from exc
        except SwitchRequiresStop as exc:
            raise DesktopError(str(exc), "stop_required", 409, serialize(exc.plan)) from exc
        except OperationBusy as exc:
            raise DesktopError(str(exc), "busy", 409) from exc
        except HubUnavailable as exc:
            raise DesktopError(str(exc), "unavailable", 503) from exc
        except HubError as exc:
            raise DesktopError(str(exc), "refused", 409) from exc
        except huggingface_auth.HuggingFaceAuthError as exc:
            # Remote errors must not echo a provided credential.
            raise DesktopError(
                "Não foi possível autenticar no Hugging Face. Verifique o token, "
                "a conexão e o acesso ao modelo; o armazenamento requer Windows DPAPI.",
                "hf_auth",
                400,
            ) from exc
        except civitai_auth.CivitaiAuthError as exc:
            # Remote errors must not echo a provided credential.
            raise DesktopError(
                "Não foi possível autenticar no Civitai. Verifique o token e a conexão; "
                "o armazenamento requer Windows DPAPI.",
                "civitai_auth",
                400,
            ) from exc
        except web_search_settings.WebSearchSettingsError as exc:
            # Remote errors must not echo a provided credential; these never carry one.
            raise DesktopError(str(exc), "web_search", 400) from exc
        except PrerequisiteInstallError as exc:
            raise DesktopError(str(exc), exc.code, 503) from exc
        except (ValueError, TypeError) as exc:
            raise DesktopError(
                "Valores inválidos. Confira o perfil, a pasta e a decisão selecionada."
            ) from exc
        except OSError as exc:
            raise DesktopError(
                "Não foi possível acessar o recurso. Verifique permissões, "
                "espaço em disco e se a pasta ainda existe.",
                "io_error",
                500,
            ) from exc

    def _execute(self, command: str, args: dict) -> object:
        app = self.app
        if command in {"estimate", "install", "plan"}:
            selection = self._selection(args)
            profile = next(p for p in app.profiles if p.id == selection.profile_id)
            location = selection.location(profile.name)
            if command == "estimate":
                return app.storage_estimate(
                    selection.profile_id,
                    location.mode,
                    str(location.root) if location.root is not None else None,
                )
            if command == "plan":
                installation = app.resolve(location)
                backend = app.backend_factory(installation)
                plan = backend.build_plan(profile)
                executor = backend.executor(profile)
                resolution = executor.resolve(plan)
                report = executor.execute(plan, dry_run=True)
                return {
                    "plan": plan,
                    "resolution": resolution,
                    "ok": report.ok,
                    "dry_run": True,
                    "results": [
                        {
                            "target": result.component_id,
                            "outcome": result.outcome,
                            "ok": result.ok,
                            "detail": result.detail,
                            "failure": result.failure,
                        }
                        for result in report.results
                    ],
                    "action_results": [
                        {
                            "action": result.action,
                            "target": result.target,
                            "ok": result.ok,
                            "detail": result.detail,
                        }
                        for result in report.action_results
                    ],
                    "unsupported": report.unsupported,
                }
            choice = next(p for p in app.profile_choices() if p.id == selection.profile_id)
            if choice.compatible is False:
                raise DesktopError(
                    "Este perfil não é compatível com o hardware detectado.", "incompatible", 409
                )
            return {"operation_id": app.install(selection)}
        if command == "start":
            return {"operation_id": app.start_stack()}
        if command == "stop":
            return {"operation_id": app.stop_stack()}
        if command == "restart":
            return {"operation_id": app.restart_stack()}
        if command == "service_start":
            return {"operation_id": app.start_service(self._text(args, "service_id"))}
        if command == "service_stop":
            return {"operation_id": app.stop_service(self._text(args, "service_id"))}
        if command == "service_restart":
            return {"operation_id": app.restart_service(self._text(args, "service_id"))}
        if command == "repair":
            return {"operation_id": app.repair()}
        if command == "retry_installation":
            return {"operation_id": app.retry_installation(self._text(args, "operation_id"))}
        if command == "choose_chat_model":
            return {"operation_id": app.choose_chat_model(self._text(args, "model_id"))}
        if command == "import_chat_model":
            return {"operation_id": app.import_chat_model(self._text(args, "reference"))}
        if command == "remove_imported_chat_model":
            app.remove_imported_chat_model(self._text(args, "model_id"))
            return None
        if command == "verify":
            return app.verify_installation()
        if command == "open_chat":
            target = app.open_webui_target()
            if isinstance(target, LaunchRefusal):
                raise DesktopError(target.reason, target.code, 409)
            url = urlsplit(target.url)
            if (
                url.scheme not in {"http", "https"}
                or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or url.username
                or url.password
            ):
                raise DesktopError(
                    "O endereço do Open WebUI não é um endereço local aprovado.", "unsafe_url", 403
                )
            self.url_opener(target.url)
            return target
        if command == "doctor":
            installation, services, vram = app.projections()
            diagnostics = {
                "installation": installation,
                "services": services,
                "vram": vram,
                "system": app.backend.facts(),
                "verification": app.verify_installation() if app.active_root() else None,
            }
            return {"text": json.dumps(serialize(diagnostics), ensure_ascii=False, indent=2)}
        if command == "download_auth":
            profile_id = self._text(args, "profile_id")
            profile = next((p for p in app.profiles if p.id == profile_id), None)
            if profile is None:
                raise DesktopError("O perfil selecionado não está disponível.", "unknown_profile")
            providers = download_auth_providers(profile, app.catalog_specs())
            for provider in providers:
                auth = huggingface_auth if provider["provider"] == "hf" else civitai_auth
                try:
                    token, _source = auth.load_token()
                    provider["configured"] = bool(token)
                except (huggingface_auth.HuggingFaceAuthError, civitai_auth.CivitaiAuthError):
                    # An unreadable local credential needs replacement, not a network probe.
                    provider["configured"] = False
            return {"providers": providers}
        if command == "hf_status":
            return huggingface_auth.check_status().as_dict()
        if command == "hf_login":
            return huggingface_auth.authenticate_with_token(self._text(args, "token")).as_dict()
        if command == "hf_logout":
            huggingface_auth.clear_token()
            return huggingface_auth.check_status().as_dict()
        if command == "civitai_status":
            return civitai_auth.check_status().as_dict()
        if command == "civitai_login":
            return civitai_auth.authenticate_with_token(self._text(args, "token")).as_dict()
        if command == "civitai_logout":
            civitai_auth.clear_token()
            return civitai_auth.check_status().as_dict()
        if command == "web_search_status":
            return web_search_settings.load_settings().as_dict()
        if command == "web_search_configure":
            enabled = args["enabled"]
            if not isinstance(enabled, bool):
                raise DesktopError(
                    "A pesquisa web deve ser ligada ou desligada (verdadeiro/falso)."
                )
            current = web_search_settings.load_settings()
            address = args.get("searxng_url", current.searxng_url)
            if not isinstance(address, str):
                raise DesktopError("O endereço do SearXNG deve ser um texto.")
            return web_search_settings.save_settings(enabled, address).as_dict()
        if command == "brave_login":
            web_search_settings.save_brave_key(self._text(args, "token"))
            return web_search_settings.load_settings().as_dict()
        if command == "brave_logout":
            web_search_settings.clear_brave_key()
            return web_search_settings.load_settings().as_dict()
        if command == "image_search_login":
            return web_search_settings.save_image_key(
                self._text(args, "provider"), self._text(args, "token")
            ).as_dict()
        if command == "image_search_logout":
            return web_search_settings.clear_image_key(self._text(args, "provider")).as_dict()
        if command == "install_git":
            if self._text(args, "confirmation") != "install-git":
                raise DesktopError(
                    "Confirme explicitamente a instalação do Git.",
                    "confirmation_required",
                    409,
                )
            return self.git_installer()
        if command == "choose_folder":
            start = (
                self._path(args, "initial_path") if "initial_path" in args else app.active_root()
            )
            return {"path": self.folder_picker.choose_folder(start)}
        if command == "resolve_conflicts":
            selection = ConflictPolicySelection(
                self._text(args, "preflight_id"), ExternalConflictPolicy(self._text(args, "policy"))
            )
            report = app.current_conflict_preflight()
            if report is None:
                raise DesktopError(
                    "Não há decisão pendente. Inicie a instalação novamente.", "stale_conflict", 409
                )
            report.validate_selection(selection)
            return app.resolve_external_conflicts(selection)
        installation_actions = {
            "pause_installation": "pause",
            "resume_installation": "resume",
            "cancel_installation": "cancel",
        }
        if command in installation_actions:
            # A control request does not start a new operation. Nest its id so the
            # renderer keeps tracking the installation it is already observing.
            return {
                "control": app.control_installation(
                    installation_actions[command], self._text(args, "operation_id")
                )
            }
        if command == "cancel_pending":
            app.cancel_pending_installation()
            return None
        if command == "open_url":
            url = self._text(args, "url")
            allowed = {
                "https://huggingface.co/settings/tokens",
                "https://huggingface.co/login",
                "https://civitai.com/user/account",
                "https://api.search.brave.com/app/keys",
                "https://unsplash.com/oauth/applications",
                "https://www.pexels.com/api/new/",
                GIT_DOWNLOAD_URL,
            }
            allowed.update(row["source_url"] for row in self.models() if row["source_url"])
            if url not in allowed or urlsplit(url).scheme != "https":
                raise DesktopError(
                    "Este endereço não é um recurso aprovado pelo Hub.", "unsafe_url", 403
                )
            self.url_opener(url)
            return {"url": url}
        if command == "service_targets":
            return self._service_targets()
        if command == "open_service":
            service_id = self._text(args, "service_id")
            if service_id not in OPENABLE_SERVICES:
                raise DesktopError("Este serviço não tem uma página para abrir.", "refused", 409)
            if service_id == "open-webui":
                # Opening Open WebUI also syncs its integration; keep one path for it.
                return self._execute("open_chat", {})
            target = next(
                (row for row in self._service_targets() if row["service_id"] == service_id), None
            )
            if target is None or target["health"] != "UP":
                raise DesktopError(
                    "Este serviço não está em execução. Inicie os serviços e tente novamente.",
                    "not_ready",
                    409,
                )
            if target["url"] is None:
                raise DesktopError(
                    "O endereço deste serviço não é um endereço local aprovado.", "unsafe_url", 403
                )
            self.url_opener(target["url"])
            return {"url": target["url"]}
        if command == "relocate":
            old = self._path(args, "old_root", known=True)
            new = self._path(args, "new_root")
            if args.get("stop", False) and app.plan_switch(new).must_stop:
                return {"operation_id": app.stop_and_switch(new, replacing=old)}
            return app.relocate_installation(old, new)
        root = self._path(args, known=command not in {"register", "plan_switch"})
        if command == "plan_switch":
            return app.plan_switch(root)
        if command == "plan_delete":
            return app.plan_delete(root)
        if command == "delete":
            if self._text(args, "confirmation") != str(root):
                raise DesktopError(
                    "Digite exatamente o caminho completo para confirmar a exclusão.",
                    "confirmation_required",
                    409,
                )
            return {"operation_id": app.delete_installation(root, stop=args.get("stop", False))}
        if command == "forget":
            return app.forget_installation(root)
        if command in {"switch", "register"}:
            # Consent is used only when the domain plan says a running stack blocks the switch.
            if args.get("stop", False) and app.plan_switch(root).must_stop:
                return {"operation_id": app.stop_and_switch(root)}
            return (
                app.open_installation(root)
                if command == "register"
                else app.switch_installation(root)
            )
        if command == "open_folder":
            layout = StackLayout.from_root(root)
            targets = {"root": root, "logs": layout.logs, "models": layout.models}
            kind = self._text(args, "kind")
            if kind not in targets:
                raise DesktopError("Escolha a pasta da instalação, logs ou modelos.")
            target = targets[kind]
            # Never follow a replaced installation root or redirected logs/models folder.
            self._refuse_redirects(root, target)
            if not target.is_dir() or not target.resolve().is_relative_to(root.resolve()):
                raise DesktopError(
                    "Esta pasta ainda não existe ou está fora da instalação.", "missing_folder", 404
                )
            self.folder_opener(target)
            return {"path": str(target)}
        if command == "logs":
            return self._logs(root, args)
        if command == "measure":
            return self._measure(root)
        raise DesktopError("Comando desconhecido.")

    @staticmethod
    def _refuse_redirects(*paths: Path) -> None:
        if any(redirects(path) for path in paths):
            raise DesktopError(
                "A pasta contém um link para outro local e não será aberta.", "unsafe_path", 403
            )

    def managed_services_running(self) -> bool:
        """Whether the installation in use has managed services up. Unknown counts as yes."""
        try:
            status = self._active_status()
        except HubUnavailable:
            return True
        return any(item.up and item.ownership == "managed" for item in status.components)

    def _service_targets(self) -> list[dict]:
        """Each enabled service of the installation in use: where it listens and how it is."""
        app = self.app
        config = app.config
        profile = (
            None
            if config is None
            else next((item for item in app.profiles if item.id == config.profile_id), None)
        )
        if profile is None:
            return []
        status = self._active_status()
        health = {item.service_id: item.health for item in status.components}
        endpoints = app.backend.options.endpoints_for(profile)
        rows = []
        for service_id, label in SERVICE_LABELS.items():
            if not endpoints.enabled(profile, service_id):
                continue
            url = endpoints.base_url(service_id)
            rows.append(
                {
                    "service_id": service_id,
                    "label": label,
                    "url": url if _loopback_url(url) else None,
                    "health": health.get(service_id, "UNKNOWN"),
                    "openable": service_id in OPENABLE_SERVICES,
                }
            )
        return rows

    def _logs(self, root: Path, args: dict) -> dict:
        """The tail of one service log of a known installation. Reads nothing else."""
        folder = StackLayout.from_root(root).logs
        self._refuse_redirects(root, folder)
        files = []
        if folder.is_dir():
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not entry.name.endswith(".log") or redirects(Path(entry.path)):
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    files.append(
                        {
                            "service": entry.name.removesuffix(".log"),
                            "name": entry.name,
                            "size_bytes": info.st_size,
                            "modified_at": datetime.fromtimestamp(info.st_mtime, UTC),
                        }
                    )
        files.sort(key=lambda item: item["service"])
        lines = args.get("lines", LOG_LINES_DEFAULT)
        if type(lines) is not int or not 1 <= lines <= LOG_LINES_MAX:
            raise DesktopError(f"Escolha de 1 a {LOG_LINES_MAX} linhas.")
        service = args.get("service")
        if service is None and files:
            service = max(files, key=lambda item: item["modified_at"])["service"]
        result = {"root": root, "files": files, "service": service, "lines": [], "truncated": False}
        if service is None:
            return result
        chosen = next((item for item in files if item["service"] == service), None)
        if chosen is None:
            raise DesktopError("Este log não existe nesta instalação.", "missing_log", 404)
        with open(folder / chosen["name"], "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - LOG_TAIL_MAX_BYTES)
            handle.seek(start)
            text = handle.read().decode("utf-8", errors="replace")
        rows = text.splitlines()
        if start > 0 and rows:
            rows = rows[1:]  # the first row began before the window
        result["lines"] = rows[-lines:]
        result["truncated"] = start > 0 or len(rows) > lines
        return result

    def _measure(self, root: Path) -> dict:
        """Bytes and files under a known installation, never following links. May be partial."""
        self._refuse_redirects(root)
        if not root.is_dir():
            raise DesktopError("A pasta desta instalação não existe.", "missing_folder", 404)
        started = time.monotonic()
        deadline = started + MEASURE_DEADLINE_SECONDS
        total = count = 0
        partial = False
        pending = [root]
        while pending:
            if time.monotonic() >= deadline or self._stopping.is_set():
                partial = True
                break
            try:
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or (
                            getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
                        ):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        else:
                            total += info.st_size
                            count += 1
            except OSError:
                partial = True
        return {
            "root": root,
            "bytes": total,
            "files": count,
            "partial": partial,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
