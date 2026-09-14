"""Keep the profile's chat model configured for tool calling in the managed Open WebUI.

Open WebUI 0.11.3 offers every UI turn in native function-calling mode its own
builtin tools (memory, notes, knowledge, chats, tasks, calendar, automations,
time: 35 schemas, about 4,700 tokens) on top of whatever the user selected.
Under Ollama's default 4,096-token window that prompt is silently cut to its
second half, and the half that goes is the tool-calling instructions together
with the user's own tool. Measured on the Pepe root (see
``docs/testing/generic-tool-calling.md``): empty replies, calls to the wrong
builtin tool with the user's arguments, and a plain ``hi`` that already
arrives truncated.

Open WebUI's own switch for this is the model capability ``builtin_tools``.
This module keeps a model record for the profile's primary chat model with
that capability off, through the authenticated Models API, exactly as an
administrator would in Admin → Models. The record is created once and marked
as the Hub's; it is written again only when the capability was turned back on.
A record somebody else created under that id is left alone and reported. The
same short-lived local administrator token as the filter sync signs the calls.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from .open_webui_filters import OWNER_PREFIX, FilterSync, FilterSyncReport, local_admin_token

OWNER = f"{OWNER_PREFIX} chat model tool calling"
# The capability set the pinned UI saves when an administrator turns exactly
# one switch off in the model editor; every other key keeps the UI's default.
CAPABILITIES = {
    "file_context": True,
    "vision": True,
    "file_upload": True,
    "web_search": True,
    "image_generation": True,
    "code_interpreter": True,
    "terminal": True,
    "citations": True,
    "status_updates": True,
    "memory": True,
    "builtin_tools": False,
}
PUBLIC_READ = {"principal_type": "anyone", "principal_id": "*", "permission": "read"}


def chat_model_record(reference: str) -> dict:
    """The record the Hub creates for an Ollama chat model it has not seen configured."""
    return {
        "id": reference,
        "base_model_id": None,
        "name": reference,
        "meta": {"managed_by": OWNER, "capabilities": dict(CAPABILITIES)},
        "params": {},
        "access_grants": [dict(PUBLIC_READ)],
        "is_active": True,
    }


def _current_record(client: httpx.Client, reference: str) -> dict | None:
    # The pinned WebUI answers 404 for an id without a record and 401 for bad
    # auth; the two must not be confused, so only 404 means absent.
    response = client.get("/api/v1/models/model", params={"id": reference})
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _builtin_tools_off(record: dict | None) -> bool:
    capabilities = ((record or {}).get("meta") or {}).get("capabilities") or {}
    return capabilities.get("builtin_tools") is False


def configure_chat_model(client: httpx.Client, reference: str) -> FilterSync:
    """Create or update only our record for ``reference``, then read it back."""
    current = _current_record(client, reference)
    if _builtin_tools_off(current):
        return FilterSync.UNCHANGED
    if current is not None and (current.get("meta") or {}).get("managed_by") != OWNER:
        raise PermissionError(
            f"model {reference!r} has a record in Open WebUI that was not created by the Hub"
        )
    if current is None:
        result = client.post("/api/v1/models/create", json=chat_model_record(reference))
        outcome = FilterSync.INSTALLED
    else:
        meta = dict(current.get("meta") or {})
        meta["capabilities"] = {**(meta.get("capabilities") or {}), "builtin_tools": False}
        form = {
            "id": reference,
            "base_model_id": current.get("base_model_id"),
            "name": current.get("name") or reference,
            "meta": meta,
            "params": current.get("params") or {},
            "access_grants": current.get("access_grants") or [dict(PUBLIC_READ)],
            "is_active": current.get("is_active", True),
        }
        result = client.post("/api/v1/models/model/update", params={"id": reference}, json=form)
        outcome = FilterSync.UPDATED
    result.raise_for_status()
    if not _builtin_tools_off(_current_record(client, reference)):
        raise RuntimeError("Open WebUI did not retain the chat model configuration")
    # The running instance serves models from a cache that a listing refreshes;
    # without this the new record only counts after the next page load.
    client.get("/api/models").raise_for_status()
    return outcome


def sync_chat_model(
    data_dir: Path,
    base_url: str,
    reference: str,
    *,
    client_factory: Callable[[str, str], httpx.Client] | None = None,
) -> FilterSyncReport:
    """Bring the managed instance's record for the chat model up to date. Idempotent.

    Builtin tools already off → nothing is written. Absent → created. Turned
    back on under our record → updated. No administrator yet → reported, not
    attempted. An unreachable or refusing instance is reported as unavailable;
    somebody else's record under the model's id is reported as refused.
    Never raises.
    """
    token = local_admin_token(data_dir)
    if token is None:
        return FilterSyncReport(
            FilterSync.NO_ADMIN,
            "Open WebUI has no administrator yet; the chat model is configured on a later start",
        )
    factory = client_factory or (
        lambda url, bearer: httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {bearer}"}, timeout=30.0
        )
    )
    try:
        with factory(base_url, token) as client:
            outcome = configure_chat_model(client, reference)
    except PermissionError as exc:
        return FilterSyncReport(FilterSync.REFUSED, str(exc))
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return FilterSyncReport(FilterSync.UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    return FilterSyncReport(outcome, f"{reference}: {outcome.value}")


__all__ = [
    "CAPABILITIES",
    "OWNER",
    "chat_model_record",
    "configure_chat_model",
    "sync_chat_model",
]
