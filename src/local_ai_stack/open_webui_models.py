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
as the Hub's; it is written again only when the capability was turned back on
or a parameter the profile declares was changed. A record somebody else
created under that id is left alone and reported. The same short-lived local
administrator token as the filter sync signs the calls.

The profile may also ask something of the model at every request
(``models.chat.params``: Open WebUI's own model parameters, ``think`` for a
reasoning model being the case that made this necessary). Those go into the
record's ``params``, which Open WebUI's Ollama route applies to every call for
that model id -- chat turns and its own background tasks alike.

A profile may also seed a system prompt (``models.chat.system_prompt``). It is
written into the record's ``params.system`` only while the record has none, so
what an administrator writes there in Admin → Models is never overwritten.

A profile's chat variant is a second record of the same kind under its own id
and name, a preset whose ``base_model_id`` is the chat model's Ollama tag, so
it answers with the same model and shows up as a separate choice in the list.

The names the Hub writes carry the profile's version ("Capella · v5.1.0"), the only
text the model selector always shows; a newer version's label replaces an older one,
while a name an administrator chose stays. A preset the profile no longer declares is
switched off, so the selector offers exactly the profile's presets.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

import httpx

from .open_webui_filters import OWNER_PREFIX, FilterSync, FilterSyncReport, local_admin_token

OWNER = f"{OWNER_PREFIX} chat model tool calling"
# SHA-256 of the whitespace-collapsed system prompts earlier Hub versions seeded
# (profiles/capella.yaml history). A record still holding one of them was never
# edited by an administrator, so the current seed replaces it.
SUPERSEDED_SYSTEM_PROMPTS = frozenset(
    {
        "c79fb03c6916b6047368422d9993a6c29fa5056712d813e4a685d9ad9d5f732c",
        "7bfb77d41f2ec84298f70f6256fc02fe31ce0f3bcb69d54236ccaa394c45f8d2",
        "9f0a24dbaf08c384a8ff7874cb1b61dd3c524533e482f2929d7adf871eb119d8",
    }
)
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
# The tail of a label the Hub writes: " · v<MAJOR.MINOR.PATCH>".
VERSION_SUFFIX = re.compile(r" · v\d+\.\d+\.\d+$")


def versioned_label(name: str, version: str) -> str:
    """The name the model selector shows for one of the profile's records."""
    return f"{name} · v{version}"


def _hub_label(current: str | None, name: str, record_id: str) -> bool:
    """Whether the record's current name is one the Hub wrote: absent, the id it defaults
    to, the plain name, or another version's label of it. Anything else is an
    administrator's own name, which a newer label never replaces."""
    base = VERSION_SUFFIX.sub("", name)
    if not current or current in (record_id, base):
        return True
    return VERSION_SUFFIX.search(current) is not None and VERSION_SUFFIX.sub("", current) == base


def _carries_name(record: dict | None, name: str | None, record_id: str) -> bool:
    if name is None:
        return True
    current = (record or {}).get("name")
    return current == name or not _hub_label(current, name, record_id)


def chat_model_record(
    reference: str,
    params: Mapping[str, object] | None = None,
    *,
    base_model_id: str | None = None,
    name: str | None = None,
    default_feature_ids: Sequence[str] | None = None,
    filter_ids: Sequence[str] | None = None,
) -> dict:
    """The record the Hub creates for an Ollama chat model it has not seen configured.

    With ``base_model_id`` it is a preset over that model, listed as ``name``.
    ``filter_ids`` are the non-global filters the model offers (a toggle in the composer)."""
    meta: dict[str, object] = {"managed_by": OWNER, "capabilities": dict(CAPABILITIES)}
    if default_feature_ids is not None:
        meta["defaultFeatureIds"] = list(default_feature_ids)
    if filter_ids is not None:
        meta["filterIds"] = list(filter_ids)
    return {
        "id": reference,
        "base_model_id": base_model_id,
        "name": name or reference,
        "meta": meta,
        "params": dict(params or {}),
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


def _carries_params(record: dict | None, params: Mapping[str, object]) -> bool:
    """Whether the record already holds every parameter the profile declares."""
    current = (record or {}).get("params") or {}
    return all(key in current and current[key] == value for key, value in params.items())


def _normalized_prompt(text: str) -> str:
    return " ".join(text.split())


def _carries_system(record: dict | None, system_prompt: str | None) -> bool:
    """A seeded prompt is satisfied by any system prompt the record already holds,
    except one an earlier Hub version seeded verbatim, which the new text replaces."""
    if system_prompt is None:
        return True
    current = ((record or {}).get("params") or {}).get("system")
    if not (isinstance(current, str) and bool(current.strip())):
        return False
    seeded = _normalized_prompt(current)
    if seeded == _normalized_prompt(system_prompt):
        return True
    return hashlib.sha256(seeded.encode("utf-8")).hexdigest() not in SUPERSEDED_SYSTEM_PROMPTS


def _carries_default_features(
    record: dict | None, default_feature_ids: Sequence[str] | None
) -> bool:
    if default_feature_ids is None:
        return True
    return ((record or {}).get("meta") or {}).get("defaultFeatureIds") == list(default_feature_ids)


def _carries_filters(record: dict | None, filter_ids: Sequence[str] | None) -> bool:
    """Every filter the profile offers on this model is in the record (others stay)."""
    if filter_ids is None:
        return True
    current = ((record or {}).get("meta") or {}).get("filterIds") or []
    return all(item in current for item in filter_ids)


def _configured(
    record: dict | None,
    params: Mapping[str, object],
    system_prompt: str | None = None,
    base_model_id: str | None = None,
    default_feature_ids: Sequence[str] | None = None,
    filter_ids: Sequence[str] | None = None,
    name: str | None = None,
    record_id: str = "",
) -> bool:
    return (
        _builtin_tools_off(record)
        and _carries_params(record, params)
        and _carries_system(record, system_prompt)
        and _carries_default_features(record, default_feature_ids)
        and _carries_filters(record, filter_ids)
        and _carries_name(record, name, record_id)
        and (base_model_id is None or (record or {}).get("base_model_id") == base_model_id)
    )


def configure_chat_model(
    client: httpx.Client,
    reference: str,
    params: Mapping[str, object] | None = None,
    system_prompt: str | None = None,
    *,
    record_id: str | None = None,
    name: str | None = None,
    default_feature_ids: Sequence[str] | None = None,
    filter_ids: Sequence[str] | None = None,
) -> FilterSync:
    """Create or update only our record for ``reference``, then read it back.

    With ``record_id`` the record is a preset of that id over ``reference``,
    listed as ``name``; its base follows ``reference`` when the chat model changes.
    ``name`` replaces a label the Hub wrote before, never an administrator's own.
    """
    params = dict(params or {})
    base = reference if record_id is not None else None
    record_id = record_id or reference
    current = _current_record(client, record_id)
    if _configured(
        current, params, system_prompt, base, default_feature_ids, filter_ids, name, record_id
    ):
        return FilterSync.UNCHANGED
    if current is not None and (current.get("meta") or {}).get("managed_by") != OWNER:
        raise PermissionError(
            f"model {record_id!r} has a record in Open WebUI that was not created by the Hub"
        )
    seed = {} if _carries_system(current, system_prompt) else {"system": system_prompt}
    if current is None:
        result = client.post(
            "/api/v1/models/create",
            json=chat_model_record(
                record_id,
                {**params, **seed},
                base_model_id=base,
                name=name,
                default_feature_ids=default_feature_ids,
                filter_ids=filter_ids,
            ),
        )
        outcome = FilterSync.INSTALLED
    else:
        meta = dict(current.get("meta") or {})
        meta["capabilities"] = {**(meta.get("capabilities") or {}), "builtin_tools": False}
        if default_feature_ids is not None:
            meta["defaultFeatureIds"] = list(default_feature_ids)
        if filter_ids is not None:
            held = list(meta.get("filterIds") or [])
            meta["filterIds"] = held + [item for item in filter_ids if item not in held]
        form = {
            "id": record_id,
            "base_model_id": base if base is not None else current.get("base_model_id"),
            "name": (
                name
                if name is not None and _hub_label(current.get("name"), name, record_id)
                else current.get("name") or name or record_id
            ),
            "meta": meta,
            # The profile's parameters over the record's; anything else it holds stays.
            "params": {**(current.get("params") or {}), **params, **seed},
            "access_grants": current.get("access_grants") or [dict(PUBLIC_READ)],
            "is_active": current.get("is_active", True),
        }
        result = client.post("/api/v1/models/model/update", params={"id": record_id}, json=form)
        outcome = FilterSync.UPDATED
    result.raise_for_status()
    if not _configured(
        _current_record(client, record_id),
        params,
        system_prompt,
        base,
        default_feature_ids,
        filter_ids,
        name,
        record_id,
    ):
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
    params: Mapping[str, object] | None = None,
    system_prompt: str | None = None,
    record_id: str | None = None,
    name: str | None = None,
    default_feature_ids: Sequence[str] | None = None,
    filter_ids: Sequence[str] | None = None,
    presets: Sequence[str] | None = None,
    client_factory: Callable[[str, str], httpx.Client] | None = None,
) -> FilterSyncReport:
    """Bring the managed instance's record for the chat model up to date. Idempotent.

    Builtin tools already off and every declared parameter in place → nothing
    is written. Absent → created. Turned back on, or a declared parameter
    changed, under our record → updated. No administrator yet → reported, not
    attempted. An unreachable or refusing instance is reported as unavailable;
    somebody else's record under the model's id is reported as refused.
    ``record_id`` and ``name`` make it the profile's variant preset over
    ``reference`` instead. ``presets`` (the chat model's own record only) are the
    preset ids the profile declares: any other preset the Hub made is switched off.
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
            outcome = configure_chat_model(
                client,
                reference,
                params,
                system_prompt,
                record_id=record_id,
                name=name,
                default_feature_ids=default_feature_ids,
                filter_ids=filter_ids,
            )
            if presets is not None and retire_presets(client, presets):
                outcome = FilterSync.UPDATED if outcome is FilterSync.UNCHANGED else outcome
    except PermissionError as exc:
        return FilterSyncReport(FilterSync.REFUSED, str(exc))
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return FilterSyncReport(FilterSync.UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    return FilterSyncReport(outcome, f"{record_id or reference}: {outcome.value}")


def retire_presets(client: httpx.Client, keep: Iterable[str]) -> list[str]:
    """Switch off every preset the Hub made (a record over a base model) whose id is not in
    ``keep``, and return their ids. Records the Hub did not make, and the chat models'
    own records, are left alone; a switched-off preset keeps its chats and settings."""
    kept = set(keep)
    listing = client.get("/api/v1/models/all")
    listing.raise_for_status()
    retired = []
    for record in listing.json() or []:
        if (
            isinstance(record, dict)
            and (record.get("meta") or {}).get("managed_by") == OWNER
            and record.get("base_model_id")
            and record.get("id") not in kept
            and record.get("is_active", True)
        ):
            client.post(
                "/api/v1/models/model/toggle", params={"id": record["id"]}
            ).raise_for_status()
            retired.append(str(record["id"]))
    if retired:
        # The selector's cache, as after a create.
        client.get("/api/models").raise_for_status()
    return retired


__all__ = [
    "CAPABILITIES",
    "OWNER",
    "chat_model_record",
    "configure_chat_model",
    "retire_presets",
    "sync_chat_model",
    "versioned_label",
]
