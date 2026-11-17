"""Keep Open WebUI from asking the chat model for follow-ups and tags after every answer.

Open WebUI 0.12.0 runs background tasks on the chat model once an answer ends:
a title, follow-up suggestions and tags. On a card where the chat model and the
image models take turns (``runtime.exclusive_image_vram``), each extra call is a
27B request that can land beside a running sampler, and the tags and follow-ups
are two of them after every single answer. A profile that opts in
(``runtime.quiet_background_tasks``) switches those two off; title generation is
kept.

Both switches are Open WebUI config rows (``task.follow_up.enable`` and
``task.tags.enable``, ``open_webui/config.py`` ``DEFAULT_CONFIG``), read by
``routers/tasks.py`` before it calls a model. Launch environment variables
(``ENABLE_FOLLOW_UP_GENERATION``, ``ENABLE_TAGS_GENERATION``) only seed rows that
do not exist yet, so an existing installation is written through the
authenticated admin API (``/api/v1/tasks/config/update``), the same form Admin ->
Interface saves. That form requires every task field, so the current values are
read and sent back with only these two changed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from .open_webui_filters import FilterSync, FilterSyncReport, local_admin_token

CONFIG_ENDPOINT = "/api/v1/tasks/config"
# Open WebUI's form names for the two switches, off; nothing else is changed.
QUIETED_FIELDS: Mapping[str, bool] = {
    "ENABLE_FOLLOW_UP_GENERATION": False,
    "ENABLE_TAGS_GENERATION": False,
}


def quiet(config: Mapping[str, object]) -> bool:
    """Whether both background tasks are already off."""
    return all(config.get(field) is value for field, value in QUIETED_FIELDS.items())


def _task_config(response: httpx.Response) -> Mapping[str, object]:
    response.raise_for_status()
    config = response.json()
    if not isinstance(config, Mapping):
        raise ValueError(  # noqa: TRY004 - a malformed server answer, reported as UNAVAILABLE
            "Open WebUI returned a task configuration that is not an object"
        )
    return config


def configure_background_tasks(client: httpx.Client) -> FilterSync:
    """Turn the two tasks off when either is on, then read them back."""
    config = _task_config(client.get(CONFIG_ENDPOINT))
    if quiet(config):
        return FilterSync.UNCHANGED
    result = client.post(f"{CONFIG_ENDPOINT}/update", json={**config, **QUIETED_FIELDS})
    result.raise_for_status()
    if not quiet(_task_config(client.get(CONFIG_ENDPOINT))):
        raise RuntimeError("Open WebUI did not retain the background task configuration")
    return FilterSync.UPDATED


def sync_background_tasks(
    data_dir: Path,
    base_url: str,
    *,
    client_factory: Callable[[str, str], httpx.Client] | None = None,
) -> FilterSyncReport:
    """Switch follow-up and tag generation off on the managed instance. Idempotent, never raises.

    No administrator yet -> reported, not attempted; an unreachable or refusing
    instance -> unavailable (the detail names the class of the failure).
    """
    token = local_admin_token(data_dir)
    if token is None:
        return FilterSyncReport(
            FilterSync.NO_ADMIN,
            "Open WebUI has no administrator yet; background tasks are configured on a later start",
        )
    factory = client_factory or (
        lambda url, bearer: httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {bearer}"}, timeout=30.0
        )
    )
    try:
        with factory(base_url, token) as client:
            outcome = configure_background_tasks(client)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return FilterSyncReport(FilterSync.UNAVAILABLE, f"background tasks: {type(exc).__name__}")
    return FilterSyncReport(outcome, f"background tasks: {outcome.value}")


__all__ = [
    "CONFIG_ENDPOINT",
    "QUIETED_FIELDS",
    "configure_background_tasks",
    "quiet",
    "sync_background_tasks",
]
