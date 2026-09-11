"""What the API cannot be asked to do, and why it cannot.

The argument is structural rather than defensive. A request names a profile,
phases and two enumerated modes; there is no field for anything else, and this
package imports nothing that could act on the machine even if there were. These
tests hold both halves of that in place.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import local_ai_stack
from local_ai_stack import test_contracts as contracts
from local_ai_stack.test_api.payloads import RUN_SPEC_FIELDS

from .conftest import Client, ScriptedHarness, gated_script, wait_for_run

# Bound to names that pytest will not try to collect as test classes, the way
# tests/test_test_contracts.py already does.
Event = contracts.TestEvent
EventKind = contracts.TestEventKind
EventSink = contracts.TestEventSink
Phase = contracts.TestPhase
PhaseOutcome = contracts.TestPhaseOutcome
PhaseResult = contracts.TestPhaseResult
RunReport = contracts.TestRunReport
RunSpec = contracts.TestRunSpec
RunState = contracts.TestRunState


PACKAGE = Path(local_ai_stack.__file__).resolve().parent / "test_api"

# Domains that own the machine. Reaching any of them from here would make this
# API a second authority over processes, files, downloads or configuration.
FORBIDDEN_MODULES = frozenset(
    {
        "subprocess",
        "shutil",
        "psutil",
        "ctypes",
        "winreg",
        "httpx",
        "requests",
        "urllib.request",
        "webbrowser",
        "multiprocessing",
        "signal",
        "tempfile",
    }
)

# Only the frozen contract and the one address decision may be imported from
# the product. Anything else is a domain this API must not reach into.
ALLOWED_LOCAL_IMPORTS = frozenset({"test_contracts", "endpoints"})


def modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def local_imports(tree: ast.AST) -> set[str]:
    """Relative imports that leave this package, by the name they reach for."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level >= 2:
            names.add(node.module or "")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.level == 0
            and node.module.startswith("local_ai_stack.")
        ):
            names.add(node.module.split(".", 1)[1])
    return names


# --- the package cannot act -------------------------------------------------


def test_the_package_imports_nothing_that_could_touch_the_machine():
    offenders: list[str] = []
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in imported_names(tree):
            root = name.split(".")[0]
            if name in FORBIDDEN_MODULES or root in FORBIDDEN_MODULES:
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_package_reaches_only_the_frozen_contract():
    """No provisioner, launcher, downloader, registry or config writer."""
    offenders: list[str] = []
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in local_imports(tree):
            if name.split(".")[0] not in ALLOWED_LOCAL_IMPORTS:
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_harness_implementation_is_never_imported():
    """Claude 1 owns the phases; this package knows only the protocol."""
    for path in modules():
        text = path.read_text(encoding="utf-8")
        assert "local_ai_stack.e2e" not in text, path.name
        assert "from ..e2e" not in text, path.name


def test_nothing_in_the_package_builds_a_filesystem_path_from_a_run_id():
    """A run id is an identifier; joining it to a path would make it a location."""
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                segments = ast.unparse(node)
                assert "run_id" not in segments, f"{path.name}: {segments}"


# --- the request cannot express it ------------------------------------------


def test_the_request_shape_is_the_whole_vocabulary():
    """Six fields, all of them capability words, none of them system words."""
    assert RUN_SPEC_FIELDS == {
        "profile_id",
        "phases",
        "root_mode",
        "provider_mode",
        "allow_network",
        "allow_gpu",
    }
    assert RUN_SPEC_FIELDS == set(RunSpec.__dataclass_fields__)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", "powershell -c calc.exe"),
        ("cmd", ["taskkill", "/F", "/PID", "1234"]),
        ("shell", True),
        ("executable", r"C:\Windows\System32\cmd.exe"),
        ("argv", ["ollama", "serve"]),
        ("cwd", r"C:\Users\Pedro"),
        ("env", {"PATH": "C:\\evil"}),
        ("root", r"C:\Users\Pedro\AppData\Local\PepitosAIHub"),
        ("path", "/etc/passwd"),
        ("url", "https://example.com/model.safetensors"),
        ("model_url", "http://10.0.0.1/payload"),
        ("pid", 4321),
        ("port", 8080),
        ("host", "0.0.0.0"),
        ("service_id", "open-webui"),
        ("kill", True),
        ("adopt_external", True),
        ("delete_models", True),
        ("script", "import os; os.system('rd /s /q C:\\\\')"),
    ],
)
def test_a_system_level_field_is_not_part_of_the_vocabulary(client, harness, field, value):
    reply = client.create_run(**{field: value})
    assert reply.status == 400
    assert reply.json()["error"] == "invalid-request"
    assert field in reply.json()["detail"]
    assert harness.calls == [], "rejected before anything downstream was reached"


def test_a_run_carries_nothing_but_the_capability_request(client, harness):
    """Whatever the harness receives, it is only ever the frozen spec."""
    reply = client.create_run(phases=["preflight", "plan"], root_mode="configured")
    wait_for_run(client.api, reply.json()["run_id"])

    _, spec = harness.calls[0]
    assert isinstance(spec, RunSpec)
    assert spec.profile_id == "pepe"
    assert spec.root_mode.value == "configured"
    assert not hasattr(spec, "command")
    assert set(spec.__dataclass_fields__) == RUN_SPEC_FIELDS


# --- external things stay external ------------------------------------------


def test_the_api_never_names_a_service_a_process_or_a_model(client):
    """There is no route and no field through which an external thing is addressed."""
    for path in (
        "/services",
        "/services/open-webui/stop",
        "/processes",
        "/processes/1234",
        "/models",
        "/models/comfyui%2Fflux2_klein_fp8",
        "/config",
        "/cleanup",
        "/shutdown",
    ):
        assert client.get(path).status in {404, 405}, path
        assert client.post(path, body={}).status in {404, 405}, path


def test_a_run_cannot_ask_for_a_capability_the_server_withheld(client, harness):
    """Real providers, network and GPU are decisions made at startup, not per request."""
    for body in (
        {"provider_mode": "local-real"},
        {"allow_network": True},
        {"allow_gpu": True},
    ):
        assert client.create_run(**body).status == 403
    assert harness.calls == []


def test_the_only_thing_the_api_calls_is_the_harness(serve):
    """One call, one spec, one run id. The API adds no second invocation."""
    started, release = threading.Event(), threading.Event()
    harness = ScriptedHarness(gated_script(started, release))
    api = serve(harness)
    client = Client(api)
    run_id = client.create_run().json()["run_id"]
    assert started.wait(timeout=10)
    release.set()
    wait_for_run(api, run_id)

    assert len(harness.calls) == 1
    assert harness.calls[0][0] == run_id
