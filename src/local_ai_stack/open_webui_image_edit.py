"""Keep Open WebUI's image editing pointed at the profile's ComfyUI edit workflow.

Open WebUI 0.11.3 edits images itself: in a chat turn with the Image feature
on, its legacy image handler (``utils/middleware.py``
``chat_image_generation_handler``) sees the images already in the conversation
and, when ``images.edit.enable`` is set, calls ``image_edits`` instead of
generating. For the ComfyUI engine that uploads each image to ComfyUI's input
folder under a fresh uuid name, writes the uploaded names into the mapped
``image`` node, queues the configured workflow, and re-hosts the PNG it
returns as an Open WebUI file attached to the message. The Hub adds nothing to
that path; it only supplies the settings.

Those settings live in Open WebUI's own database. Launch environment variables
only seed keys that do not exist yet, and every instance that has started once
already has ``images.edit.*`` rows, so they are written through the
authenticated admin API (``/api/v1/images/config``), the same form the Admin →
Images page saves: the current form is read, only the edit fields the Hub owns
are changed, and the whole form goes back, so every other setting is kept.

Ownership travels inside the value itself: the workflow the Hub writes carries
``_meta.managed_by`` on its image input node. A configuration with that mark
is the Hub's and is repaired when it drifted. One that was never set up (edit
disabled, no ComfyUI edit workflow) is claimed. Anything else -- someone's own
edit engine or workflow -- is left untouched and reported as refused.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path

import httpx

from .open_webui_filters import OWNER_PREFIX, FilterSync, FilterSyncReport, local_admin_token
from .service_config import ImageEditSettings

OWNER = f"{OWNER_PREFIX} image editing"
CONFIG_ENDPOINT = "/api/v1/images/config"
ENGINE = "comfyui"


def managed_workflow(settings: ImageEditSettings) -> dict:
    """The graph as the Hub stores it: the workflow, marked on its image input node."""
    graph = deepcopy(dict(settings.workflow))
    image_nodes = [
        node_id
        for entry in settings.nodes
        if entry.get("type") == "image"
        for node_id in entry.get("node_ids", [])
    ]
    if not image_nodes or image_nodes[0] not in graph:
        raise ValueError("the edit workflow has no mapped image input node to carry ownership")
    node = graph[image_nodes[0]]
    node["_meta"] = {**(node.get("_meta") or {}), "managed_by": OWNER}
    return graph


def desired_fields(settings: ImageEditSettings) -> dict:
    """The edit fields the Hub owns, as the admin form names them."""
    return {
        "ENABLE_IMAGE_EDIT": True,
        "IMAGE_EDIT_ENGINE": ENGINE,
        "IMAGE_EDIT_MODEL": settings.model,
        "IMAGES_EDIT_COMFYUI_BASE_URL": settings.base_url.rstrip("/"),
        "IMAGES_EDIT_COMFYUI_WORKFLOW": json.dumps(managed_workflow(settings)),
        "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES": [dict(node) for node in settings.nodes],
    }


def _parsed_workflow(value: object) -> dict | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def owned_by_hub(config: Mapping[str, object]) -> bool:
    workflow = _parsed_workflow(config.get("IMAGES_EDIT_COMFYUI_WORKFLOW"))
    return workflow is not None and any(
        isinstance(node, dict)
        and str((node.get("_meta") or {}).get("managed_by", "")).startswith(OWNER_PREFIX)
        for node in workflow.values()
    )


def unclaimed(config: Mapping[str, object]) -> bool:
    """Image editing was never set up: off, and no ComfyUI edit workflow."""
    workflow = config.get("IMAGES_EDIT_COMFYUI_WORKFLOW")
    return not config.get("ENABLE_IMAGE_EDIT") and not (
        isinstance(workflow, str) and workflow.strip()
    )


def matches(config: Mapping[str, object], desired: Mapping[str, object]) -> bool:
    for field, value in desired.items():
        current = config.get(field)
        if field == "IMAGES_EDIT_COMFYUI_WORKFLOW":
            if _parsed_workflow(current) != json.loads(str(value)):
                return False
        elif field == "IMAGES_EDIT_COMFYUI_BASE_URL":
            if str(current or "").rstrip("/") != value:
                return False
        elif current != value:
            return False
    return True


def configure_image_edit(client: httpx.Client, settings: ImageEditSettings) -> FilterSync:
    """Claim or repair only the Hub's edit settings, then read them back."""
    response = client.get(CONFIG_ENDPOINT)
    response.raise_for_status()
    config = response.json()
    desired = desired_fields(settings)
    if matches(config, desired):
        return FilterSync.UNCHANGED
    if owned_by_hub(config):
        outcome = FilterSync.UPDATED
    elif unclaimed(config):
        outcome = FilterSync.INSTALLED
    else:
        raise PermissionError(
            "Open WebUI's image editing is configured by someone else "
            f"(engine {config.get('IMAGE_EDIT_ENGINE')!r}); it was left untouched"
        )
    result = client.post(f"{CONFIG_ENDPOINT}/update", json={**config, **desired})
    result.raise_for_status()
    verified = client.get(CONFIG_ENDPOINT)
    verified.raise_for_status()
    if not matches(verified.json(), desired):
        raise RuntimeError("Open WebUI did not retain the image edit configuration")
    return outcome


def sync_image_edit(
    data_dir: Path,
    base_url: str,
    settings: ImageEditSettings,
    *,
    client_factory: Callable[[str, str], httpx.Client] | None = None,
) -> FilterSyncReport:
    """Bring the managed instance's image edit settings up to date. Idempotent.

    Same settings → nothing is written. Never set up → claimed. The Hub's and
    drifted → repaired. No administrator yet → reported, not attempted.
    Someone else's configuration → refused. Unreachable or refusing instance
    → unavailable. Never raises.
    """
    token = local_admin_token(data_dir)
    if token is None:
        return FilterSyncReport(
            FilterSync.NO_ADMIN,
            "Open WebUI has no administrator yet; image editing is configured on a later start",
        )
    factory = client_factory or (
        lambda url, bearer: httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {bearer}"}, timeout=30.0
        )
    )
    try:
        with factory(base_url, token) as client:
            outcome = configure_image_edit(client, settings)
    except PermissionError as exc:
        return FilterSyncReport(FilterSync.REFUSED, str(exc))
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return FilterSyncReport(FilterSync.UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    return FilterSyncReport(outcome, f"image editing: {outcome.value}")


__all__ = [
    "ENGINE",
    "OWNER",
    "configure_image_edit",
    "desired_fields",
    "managed_workflow",
    "matches",
    "owned_by_hub",
    "sync_image_edit",
    "unclaimed",
]
