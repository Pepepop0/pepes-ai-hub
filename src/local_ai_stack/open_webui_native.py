"""Versioned native image extension, installed only in the managed environment.

No Open WebUI imports run during provisioning: package metadata locates the wheel
inside the owned virtual environment, and only its main.py/frontend are patched.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
from pathlib import Path

from .pyenv import PythonEnvironment
from .resources import resource_directory

SUPPORTED_OPEN_WEBUI = "0.12.0"
EXTENSION_VERSION = "1"
MAIN_BEGIN = "# BEGIN PEPITO NATIVE IMAGE EXTENSION"
MAIN_END = "# END PEPITO NATIVE IMAGE EXTENSION"
HTML_BEGIN = "<!-- BEGIN PEPITO NATIVE IMAGE EXTENSION -->"
HTML_END = "<!-- END PEPITO NATIVE IMAGE EXTENSION -->"
ROUTER_ANCHOR = "app.include_router(images.router, prefix='/api/v1/images', tags=['images'])"
ASSET_NAMES = (
    "pepito_native.py",
    "pepito_calibration.py",
    "pepito_image_preservation.py",
    "pepito_edit_methods.py",
    "pepito_swap.py",
    "pepito_swap_compose.py",
    "pepito_image_ui.js",
    "pepito_image_ui.css",
)


class NativeExtensionError(RuntimeError):
    """The pinned wheel or its managed source does not match our integration."""


def _strip_owned(text: str, begin: str, end: str) -> str:
    if begin not in text and end not in text:
        return text
    if text.count(begin) != 1 or text.count(end) != 1:
        raise NativeExtensionError(
            "The managed native extension contains ambiguous ownership markers."
        )
    first, remainder = text.split(begin, 1)
    if end not in remainder:
        raise NativeExtensionError(
            "The managed native extension ownership markers are out of order."
        )
    owned, last = remainder.split(end, 1)
    if not owned.startswith("\n"):
        raise NativeExtensionError("The managed native extension ownership block is malformed.")
    return first + last.removeprefix("\n")


def _atomic_write(path: Path, content: bytes) -> bool:
    """Write atomically; whether the file changed."""
    if path.is_file() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=path.name + ".pepito-", suffix=".tmp", delete=False
        ) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return True


def install_native_extension(
    package_dir: Path, *, version: str, assets: Path | None = None
) -> bool:
    """Synchronize all assets even when the package already has the desired pin.

    Validate every source and resource before writing anything. An unsupported pin
    or changed upstream integration point is a repair error, never a false success.
    """
    if version != SUPPORTED_OPEN_WEBUI:
        raise NativeExtensionError(
            f"Pepito's native image extension requires Open WebUI {SUPPORTED_OPEN_WEBUI}; found {version!r}."
        )
    package_dir = Path(package_dir)
    assets = resource_directory("assets") / "open-webui" if assets is None else Path(assets)
    main_path = package_dir / "main.py"
    frontend = package_dir / "frontend"
    index_path = frontend / "index.html"
    try:
        main = _strip_owned(main_path.read_text(encoding="utf-8"), MAIN_BEGIN, MAIN_END)
        index = _strip_owned(index_path.read_text(encoding="utf-8"), HTML_BEGIN, HTML_END)
        resources = {name: (assets / name).read_bytes() for name in ASSET_NAMES}
    except (OSError, UnicodeError) as exc:
        raise NativeExtensionError(
            f"Cannot read the managed Open WebUI source or bundled native assets: {exc}"
        ) from exc
    if main.count(ROUTER_ANCHOR) != 1 or index.count("</head>") != 1:
        raise NativeExtensionError(
            "Open WebUI source does not match the supported 0.12.0 router/frontend integration points. Reinstall the pinned managed package."
        )
    try:
        tree = ast.parse(main)
    except SyntaxError as exc:
        raise NativeExtensionError("Managed Open WebUI main.py is not valid Python.") from exc
    anchor_line = main[: main.index(ROUTER_ANCHOR)].count("\n") + 1
    if not any(
        isinstance(node, ast.Expr)
        and node.lineno == anchor_line
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "include_router"
        for node in tree.body
    ):
        raise NativeExtensionError(
            "The Open WebUI images router is no longer a top-level integration point."
        )
    main_block = f"{MAIN_BEGIN}\nfrom open_webui.pepito_native import router as pepito_native_router\napp.include_router(pepito_native_router)\n{MAIN_END}\n"
    main = main.replace(ROUTER_ANCHOR, main_block + ROUTER_ANCHOR)
    html_block = f'{HTML_BEGIN}\n<link rel="stylesheet" href="/pepito-native/v{EXTENSION_VERSION}/pepito_image_ui.css">\n<script defer src="/pepito-native/v{EXTENSION_VERSION}/pepito_image_ui.js"></script>\n{HTML_END}\n'
    index = index.replace("</head>", html_block + "</head>")
    # Versioned public assets are served by upstream's existing frontend mount;
    # the authenticated API router must be registered before that catch-all mount.
    destination = frontend / "pepito-native" / f"v{EXTENSION_VERSION}"
    changed = False
    for name in ASSET_NAMES:
        target = package_dir / name if name.endswith(".py") else destination / name
        changed |= _atomic_write(target, resources[name])
    changed |= _atomic_write(main_path, main.encode("utf-8"))
    changed |= _atomic_write(index_path, index.encode("utf-8"))
    return changed


def sync_native_extension(context, environment: PythonEnvironment, *, version: str) -> bool:
    """Resolve the installed wheel without importing it or loading any model."""
    if version != SUPPORTED_OPEN_WEBUI:
        raise NativeExtensionError(
            f"Native images support only Open WebUI {SUPPORTED_OPEN_WEBUI}; requested {version!r}."
        )
    code = (
        "import importlib.metadata,json; "
        "d=importlib.metadata.distribution('open-webui'); "
        "print(json.dumps({'version': d.version, 'package': str(d.locate_file('open_webui'))}))"
    )
    result = context.runner.run([str(environment.python), "-c", code])
    if not result.ok:
        raise NativeExtensionError(
            "Cannot locate the managed Open WebUI distribution for native extension repair."
        )
    try:
        metadata = json.loads(result.stdout.strip())
        installed = metadata["version"]
        package = Path(metadata["package"]).resolve()
    except (ValueError, KeyError, TypeError) as exc:
        raise NativeExtensionError(
            "Managed Open WebUI returned invalid distribution metadata."
        ) from exc
    if installed != version:
        raise NativeExtensionError(
            f"Managed Open WebUI pin mismatch: expected {version}, found {installed}."
        )
    # Do not trust a relocated interpreter or malformed package metadata to point
    # this narrowly-owned installer at a global/external Open WebUI installation.
    if not package.is_relative_to(environment.root.resolve()) or package.name != "open_webui":
        raise NativeExtensionError(
            "Open WebUI package is outside the managed virtual environment; leaving it untouched."
        )
    return install_native_extension(package, version=installed)
