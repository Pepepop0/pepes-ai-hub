"""Keeping Open WebUI's image editing pointed at the managed ComfyUI edit workflow.

A fake of the pinned admin Images API (``/api/v1/images/config``, whole-form
updates) records every write. The settings come from the real manifest and
the real embedded graph. The sync must claim a configuration nobody set up,
leave its own current one alone, repair its own when it drifted, refuse
somebody else's, keep every other image setting exactly as it was, and report
instead of raising.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from test_open_webui_filters import data_dir, factory_for
from test_workflows import image_resolver_repo

from local_ai_stack.endpoints import StackEndpoints
from local_ai_stack.open_webui_filters import FilterSync
from local_ai_stack.open_webui_image_edit import (
    OWNER,
    configure_image_edit,
    desired_fields,
    managed_workflow,
    owned_by_hub,
    sync_image_edit,
    unclaimed,
)
from local_ai_stack.profiles import load_profile
from local_ai_stack.service_config import ImageEditSettings, open_webui_image_edit_settings
from local_ai_stack.workflows import load_workflow_manifest, resolve_workflow

REPO = Path(__file__).resolve().parents[1]

# What Open WebUI 0.11.3 stores on an instance that has started once (the
# seeded defaults), with image generation already configured by the Hub.
SEEDED = {
    "ENABLE_IMAGE_GENERATION": True,
    "ENABLE_IMAGE_PROMPT_GENERATION": True,
    "IMAGE_GENERATION_ENGINE": "comfyui",
    "IMAGE_GENERATION_MODEL": "flux-2-klein-4b-fp8.safetensors",
    "IMAGE_SIZE": "512x512",
    "IMAGE_STEPS": 50,
    "IMAGES_OPENAI_API_BASE_URL": "https://api.openai.com/v1",
    "IMAGES_OPENAI_API_KEY": "",
    "IMAGES_OPENAI_API_VERSION": "",
    "IMAGES_OPENAI_API_PARAMS": {},
    "AUTOMATIC1111_BASE_URL": "",
    "AUTOMATIC1111_API_AUTH": "",
    "AUTOMATIC1111_PARAMS": {},
    "COMFYUI_BASE_URL": "http://127.0.0.1:8188",
    "COMFYUI_API_KEY": "",
    "COMFYUI_WORKFLOW": '{"9": {"class_type": "SaveImage", "inputs": {}}}',
    "COMFYUI_WORKFLOW_NODES": [{"type": "prompt", "key": "value", "node_ids": ["76"]}],
    "IMAGES_GEMINI_API_BASE_URL": "",
    "IMAGES_GEMINI_API_KEY": "",
    "IMAGES_GEMINI_ENDPOINT_METHOD": "",
    "ENABLE_IMAGE_EDIT": False,
    "IMAGE_EDIT_ENGINE": "openai",
    "IMAGE_EDIT_MODEL": "",
    "IMAGE_EDIT_SIZE": "",
    "IMAGES_EDIT_OPENAI_API_BASE_URL": "https://api.openai.com/v1",
    "IMAGES_EDIT_OPENAI_API_KEY": "",
    "IMAGES_EDIT_OPENAI_API_VERSION": "",
    "IMAGES_EDIT_GEMINI_API_BASE_URL": "",
    "IMAGES_EDIT_GEMINI_API_KEY": "",
    "IMAGES_EDIT_COMFYUI_BASE_URL": "",
    "IMAGES_EDIT_COMFYUI_API_KEY": "",
    "IMAGES_EDIT_COMFYUI_WORKFLOW": "",
    "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES": [],
}


class FakeImagesApi:
    """The pinned admin Images API: a whole form in, the stored form out."""

    def __init__(self, config: dict | None = None, *, authorized: bool = True) -> None:
        self.config = deepcopy(config if config is not None else SEEDED)
        self.writes: list[dict] = []
        self.authorized = authorized

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.authorized or not request.headers.get("Authorization"):
            return httpx.Response(401, json={"detail": "Not authenticated"})
        if request.method == "GET" and request.url.path == "/api/v1/images/config":
            return httpx.Response(200, json=deepcopy(self.config))
        if request.method == "POST" and request.url.path == "/api/v1/images/config/update":
            form = json.loads(request.content)
            missing = set(SEEDED) - set(form)
            if missing:  # pydantic would refuse a partial form
                return httpx.Response(422, json={"detail": sorted(missing)})
            self.writes.append(form)
            form["IMAGES_EDIT_COMFYUI_BASE_URL"] = form["IMAGES_EDIT_COMFYUI_BASE_URL"].strip("/")
            self.config = {key: form[key] for key in SEEDED}
            return httpx.Response(200, json=deepcopy(self.config))
        pytest.fail(f"unexpected call {request.method} {request.url.path}")

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://127.0.0.1:8080",
            headers={"Authorization": "Bearer test"},
            transport=httpx.MockTransport(self),
        )


@pytest.fixture(scope="module")
def settings() -> ImageEditSettings:
    """The settings the Hub derives for Pepe from the shipped manifest and graph."""
    pepe = load_profile(REPO / "profiles" / "pepe.yaml")
    spec = load_workflow_manifest(REPO / "manifests" / "workflows.yaml")["flux2_klein_edit"]
    resolution = resolve_workflow(spec, pepe, image_resolver_repo(), repo_root=REPO)
    graph = json.loads((REPO / spec.file).read_text(encoding="utf-8"))
    built, problems = open_webui_image_edit_settings(
        StackEndpoints.from_profile(pepe), resolution, graph
    )
    assert problems == ()
    return built


def other_fields(config: dict) -> dict:
    ours = set(desired_fields_keys())
    return {key: value for key, value in config.items() if key not in ours}


def desired_fields_keys() -> tuple[str, ...]:
    return (
        "ENABLE_IMAGE_EDIT",
        "IMAGE_EDIT_ENGINE",
        "IMAGE_EDIT_MODEL",
        "IMAGES_EDIT_COMFYUI_BASE_URL",
        "IMAGES_EDIT_COMFYUI_WORKFLOW",
        "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES",
    )


# --- what the Hub asks Open WebUI for ---------------------------------------------------


def test_the_settings_map_only_what_open_webuis_edit_path_fills_in(settings):
    assert settings.base_url == "http://127.0.0.1:8188"
    assert settings.model == "flux-2-klein-4b-fp8.safetensors"
    assert list(settings.nodes) == [
        {"type": "image", "key": "image", "node_ids": ["76"]},
        {"type": "model", "key": "unet_name", "node_ids": ["75:70"]},
        {"type": "prompt", "key": "text", "node_ids": ["75:74"]},
        {"type": "seed", "key": "noise_seed", "node_ids": ["75:73"]},
    ]


def test_ownership_is_marked_on_the_image_node_and_nowhere_else(settings):
    graph = managed_workflow(settings)
    marked = [node_id for node_id, node in graph.items() if "managed_by" in node.get("_meta", {})]
    assert marked == ["76"]
    assert graph["76"]["_meta"]["managed_by"] == OWNER
    assert "managed_by" not in json.dumps(dict(settings.workflow)), "the shipped graph is untouched"


def test_a_graph_mapping_steps_or_lacking_an_image_is_not_configured(settings):
    pepe = load_profile(REPO / "profiles" / "pepe.yaml")
    specs = load_workflow_manifest(REPO / "manifests" / "workflows.yaml")
    from dataclasses import replace

    from local_ai_stack.workflows import NodeBinding, WorkflowParameter

    edit = specs["flux2_klein_edit"]
    graph = dict(settings.workflow)
    endpoints = StackEndpoints.from_profile(pepe)
    with_steps = replace(
        edit, bindings={**edit.bindings, WorkflowParameter.STEPS: NodeBinding("75:62", "steps")}
    )
    no_image = replace(
        edit,
        bindings={k: v for k, v in edit.bindings.items() if k is not WorkflowParameter.IMAGE},
    )
    for spec, needle in ((with_steps, "steps"), (no_image, "image and a prompt")):
        resolution = resolve_workflow(spec, pepe, image_resolver_repo(), repo_root=REPO)
        built, problems = open_webui_image_edit_settings(endpoints, resolution, graph)
        assert built is None and any(needle in problem for problem in problems)
    unmapped = resolve_workflow(
        replace(edit, unresolved=("image",)), pepe, image_resolver_repo(), repo_root=REPO
    )
    built, problems = open_webui_image_edit_settings(endpoints, unmapped, graph)
    assert built is None and "mapping-incomplete" in problems[0]


# --- H, I, J: idempotent, repairs its own drift, never takes someone else's ----------------


def test_h_a_fresh_instance_is_claimed_once_and_then_left_alone(settings):
    api = FakeImagesApi()
    with api.client() as client:
        assert configure_image_edit(client, settings) is FilterSync.INSTALLED
        assert configure_image_edit(client, settings) is FilterSync.UNCHANGED
        assert configure_image_edit(client, settings) is FilterSync.UNCHANGED
    assert len(api.writes) == 1
    config = api.config
    assert config["ENABLE_IMAGE_EDIT"] is True and config["IMAGE_EDIT_ENGINE"] == "comfyui"
    assert config["IMAGE_EDIT_MODEL"] == "flux-2-klein-4b-fp8.safetensors"
    assert config["IMAGES_EDIT_COMFYUI_BASE_URL"] == "http://127.0.0.1:8188"
    assert json.loads(config["IMAGES_EDIT_COMFYUI_WORKFLOW"]) == managed_workflow(settings)
    assert config["IMAGES_EDIT_COMFYUI_WORKFLOW_NODES"] == list(settings.nodes)
    # Image generation and every other setting went back exactly as they were.
    assert other_fields(config) == other_fields(SEEDED)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ENABLE_IMAGE_EDIT", False),
        ("IMAGE_EDIT_ENGINE", "openai"),
        ("IMAGE_EDIT_MODEL", ""),
        ("IMAGES_EDIT_COMFYUI_BASE_URL", "http://127.0.0.1:9999"),
        (
            "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES",
            [{"type": "image", "key": "image", "node_ids": ["9999"]}],
        ),
    ],
)
def test_i_the_hubs_own_configuration_is_repaired_when_it_drifted(settings, field, value):
    api = FakeImagesApi()
    with api.client() as client:
        configure_image_edit(client, settings)
        api.config[field] = value
        assert configure_image_edit(client, settings) is FilterSync.UPDATED
        assert configure_image_edit(client, settings) is FilterSync.UNCHANGED
    assert api.config[field] != value
    assert other_fields(api.config) == other_fields(SEEDED)


def test_i_a_drifted_workflow_that_still_carries_the_mark_is_repaired(settings):
    api = FakeImagesApi()
    with api.client() as client:
        configure_image_edit(client, settings)
        stale = json.loads(api.config["IMAGES_EDIT_COMFYUI_WORKFLOW"])
        stale["75:62"]["inputs"]["steps"] = 50
        api.config["IMAGES_EDIT_COMFYUI_WORKFLOW"] = json.dumps(stale)
        assert owned_by_hub(api.config)
        assert configure_image_edit(client, settings) is FilterSync.UPDATED
    assert json.loads(api.config["IMAGES_EDIT_COMFYUI_WORKFLOW"])["75:62"]["inputs"]["steps"] == 4


@pytest.mark.parametrize(
    "foreign",
    [
        {
            "ENABLE_IMAGE_EDIT": True,
            "IMAGE_EDIT_ENGINE": "openai",
            "IMAGES_EDIT_OPENAI_API_KEY": "sk-user",
        },
        {
            "IMAGE_EDIT_ENGINE": "comfyui",
            "IMAGES_EDIT_COMFYUI_WORKFLOW": '{"1": {"class_type": "LoadImage", "inputs": {"image": "x"}}}',
        },
        {"IMAGES_EDIT_COMFYUI_WORKFLOW": "not even json"},
    ],
    ids=["enabled-openai", "users-comfyui-workflow", "unparseable-workflow"],
)
def test_j_somebody_elses_edit_configuration_is_refused_untouched(settings, foreign):
    api = FakeImagesApi({**SEEDED, **foreign})
    before = deepcopy(api.config)
    assert not unclaimed(api.config) and not owned_by_hub(api.config)
    with api.client() as client, pytest.raises(PermissionError, match="someone else"):
        configure_image_edit(client, settings)
    assert api.writes == [] and api.config == before


def test_a_disabled_openai_setup_without_a_workflow_is_claimed_keeping_its_key(settings):
    api = FakeImagesApi({**SEEDED, "IMAGES_EDIT_OPENAI_API_KEY": "sk-kept"})
    with api.client() as client:
        assert configure_image_edit(client, settings) is FilterSync.INSTALLED
    assert api.config["IMAGES_EDIT_OPENAI_API_KEY"] == "sk-kept"


def test_an_authentication_failure_writes_nothing(settings):
    api = FakeImagesApi(authorized=False)
    with api.client() as client, pytest.raises(httpx.HTTPStatusError):
        configure_image_edit(client, settings)
    assert api.writes == []


def test_a_configuration_the_instance_does_not_retain_is_an_error(settings):
    class Forgetful(FakeImagesApi):
        def __call__(self, request):
            response = super().__call__(request)
            if request.method == "POST":
                self.config["ENABLE_IMAGE_EDIT"] = False
            return response

    api = Forgetful()
    with api.client() as client, pytest.raises(RuntimeError, match="did not retain"):
        configure_image_edit(client, settings)


def test_the_desired_fields_are_exactly_the_ones_the_hub_owns(settings):
    assert tuple(desired_fields(settings)) == desired_fields_keys()


# --- sync: the product-facing outcome --------------------------------------------------


def test_sync_installs_then_reports_unchanged(tmp_path, settings):
    api = FakeImagesApi()
    factory = factory_for(api)
    directory = data_dir(tmp_path)
    first = sync_image_edit(directory, "http://127.0.0.1:8080", settings, client_factory=factory)
    second = sync_image_edit(directory, "http://127.0.0.1:8080", settings, client_factory=factory)
    assert (first.outcome, second.outcome) == (FilterSync.INSTALLED, FilterSync.UNCHANGED)
    assert first.changed and not second.changed and first.ok and second.ok


def test_sync_reports_refused_no_admin_and_unavailable_without_raising(tmp_path, settings):
    foreign = FakeImagesApi({**SEEDED, "ENABLE_IMAGE_EDIT": True})
    refused = sync_image_edit(
        data_dir(tmp_path), "http://x", settings, client_factory=factory_for(foreign)
    )
    assert refused.outcome is FilterSync.REFUSED and not refused.ok and foreign.writes == []

    waiting = sync_image_edit(
        data_dir(tmp_path / "b", admin=False),
        "http://x",
        settings,
        client_factory=factory_for(FakeImagesApi()),
    )
    assert waiting.outcome is FilterSync.NO_ADMIN and waiting.ok

    def refusing(url: str, bearer: str) -> httpx.Client:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        return httpx.Client(base_url=url, transport=httpx.MockTransport(explode))

    down = sync_image_edit(data_dir(tmp_path / "c"), "http://x", settings, client_factory=refusing)
    assert down.outcome is FilterSync.UNAVAILABLE and "ConnectError" in down.detail
