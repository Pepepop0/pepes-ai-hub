"""Deploy the image chat fix through Open WebUI's authenticated Functions API.

No database edits, dependency patching, global sync, or service restart. The
caller supplies an already authenticated httpx client for the managed WebUI.
"""

from pathlib import Path

import httpx

FILTER_ID = "pepito_image_chat"
OWNER = "Pepito's AI Hub: native image chat compatibility"


def install_image_chat_filter(client: httpx.Client) -> bool:
    """Create/update only our filter, activate it, and verify. Return changed."""
    source = Path(__file__).with_name("open_webui_image_filter.py").read_text(encoding="utf-8")
    endpoint = f"/api/v1/functions/id/{FILTER_ID}"
    # The pinned WebUI returns 401 for a missing ID as well as bad auth. List
    # first so an authentication failure can never be mistaken for absence.
    listing = client.get("/api/v1/functions/list")
    listing.raise_for_status()
    changed = False
    if not any(item.get("id") == FILTER_ID for item in listing.json()):
        current = None
    else:
        response = client.get(endpoint)
        response.raise_for_status()
        current = response.json()
    if current and current.get("meta", {}).get("description") != OWNER:
        raise ValueError("The image filter ID is already owned by another function.")
    form = {
        "id": FILTER_ID,
        "name": "Pepito Image Chat",
        "content": source,
        "meta": {"description": OWNER},
    }
    if current is None or current.get("content") != source:
        result = client.post(
            endpoint + "/update" if current else "/api/v1/functions/create", json=form
        )
        result.raise_for_status()
        current = result.json()
        changed = True
    for field, suffix in (("is_active", "/toggle"), ("is_global", "/toggle/global")):
        if not current.get(field):
            result = client.post(endpoint + suffix)
            result.raise_for_status()
            current = result.json()
            changed = True
    verified = client.get(endpoint)
    verified.raise_for_status()
    record = verified.json()
    if not (
        record.get("content") == source
        and record.get("type") == "filter"
        and record.get("is_active")
        and record.get("is_global")
    ):
        raise ValueError("Open WebUI did not retain the image chat filter configuration.")
    return changed
