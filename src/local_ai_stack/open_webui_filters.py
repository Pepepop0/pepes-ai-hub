"""Keep the Hub's image chat filter installed in the managed Open WebUI.

The filter (``assets/open-webui/pepito_image_chat.py``, a product resource:
it is uploaded, never imported) is a Filter Function that Open WebUI stores in
its own database and loads on demand. It is deployed through
the authenticated Functions API -- never by editing that database -- and only
ever to the single id this module owns. A function somebody else created under
that id is refused, not overwritten; every other function is never touched.

Authentication is local: the managed instance's own secret key signs a
short-lived token for its first administrator, the same way the instance
signs its sessions. No password is stored or asked for. Before an administrator
exists (a fresh install nobody has signed into yet) there is nothing to
authenticate as, and the sync reports that instead of guessing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from .resources import resource_directory

FILTER_ID = "pepito_image_chat"
FILTER_NAME = "Pepito Image Chat"
# Ownership is the prefix: a function whose description does not start with
# it was not installed by the Hub, whatever its id says.
OWNER_PREFIX = "Pepito's AI Hub:"
OWNER = f"{OWNER_PREFIX} image chat routing"
SECRET_KEY_FILE = ".webui_secret_key"
DATABASE_FILE = "webui.db"
TOKEN_TTL_SECONDS = 120
# Bundled with the other resources, so a frozen build finds it too.
FILTER_RESOURCE = Path("open-webui") / "pepito_image_chat.py"
# The "Alta qualidade", "Arte digital", "Fotorrealista" and "Buscar referências" switches:
# filters that do nothing themselves.
# Open WebUI lists a `toggle` filter in the composer's Integrations menu for the models
# that name it, and the image chat filter reads whether it is on from the request's
# ``filter_ids``.
QUALITY_FILTER_ID = "pepito_image_quality"
QUALITY_FILTER_NAME = "Alta qualidade"
QUALITY_OWNER = f"{OWNER_PREFIX} high quality switch"
QUALITY_RESOURCE = Path("open-webui") / "pepito_image_quality.py"
ART_FILTER_ID = "pepito_image_art"
ART_FILTER_NAME = "Arte digital"
ART_OWNER = f"{OWNER_PREFIX} digital art switch"
ART_RESOURCE = Path("open-webui") / "pepito_image_art.py"
PHOTO_FILTER_ID = "pepito_image_photo"
PHOTO_FILTER_NAME = "Fotorrealista"
PHOTO_OWNER = f"{OWNER_PREFIX} photorealistic switch"
PHOTO_RESOURCE = Path("open-webui") / "pepito_image_photo.py"
REFS_FILTER_ID = "pepito_image_refs"
REFS_FILTER_NAME = "Buscar referências"
REFS_OWNER = f"{OWNER_PREFIX} reference search switch"
REFS_RESOURCE = Path("open-webui") / "pepito_image_refs.py"


class FilterSync(StrEnum):
    INSTALLED = "installed"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    NO_ADMIN = "no-admin"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"


@dataclass(frozen=True)
class FilterSyncReport:
    outcome: FilterSync
    detail: str = ""

    @property
    def changed(self) -> bool:
        return self.outcome in (FilterSync.INSTALLED, FilterSync.UPDATED)

    @property
    def ok(self) -> bool:
        """The filter is in place, or legitimately cannot be yet."""
        return self.outcome in (
            FilterSync.INSTALLED,
            FilterSync.UPDATED,
            FilterSync.UNCHANGED,
            FilterSync.NO_ADMIN,
        )


def filter_path() -> Path:
    return resource_directory("assets") / FILTER_RESOURCE


def filter_source() -> str:
    return filter_path().read_text(encoding="utf-8")


def quality_filter_source() -> str:
    return (resource_directory("assets") / QUALITY_RESOURCE).read_text(encoding="utf-8")


def art_filter_source() -> str:
    return (resource_directory("assets") / ART_RESOURCE).read_text(encoding="utf-8")


def photo_filter_source() -> str:
    return (resource_directory("assets") / PHOTO_RESOURCE).read_text(encoding="utf-8")


def _install_function(
    client: httpx.Client,
    function_id: str,
    name: str,
    source: str,
    owner: str,
    *,
    is_global: bool,
) -> FilterSync:
    """Create or update only the Hub's function ``function_id`` and read it back."""
    endpoint = f"/api/v1/functions/id/{function_id}"
    # The pinned WebUI returns 401 for a missing id as well as for bad auth.
    # List first so an authentication failure is never mistaken for absence.
    listing = client.get("/api/v1/functions/list")
    listing.raise_for_status()
    current = None
    if any(item.get("id") == function_id for item in listing.json()):
        response = client.get(endpoint)
        response.raise_for_status()
        current = response.json()
    if current and not str(current.get("meta", {}).get("description", "")).startswith(OWNER_PREFIX):
        raise PermissionError(f"function {function_id!r} exists but was not installed by the Hub")
    outcome = FilterSync.UNCHANGED
    form = {
        "id": function_id,
        "name": name,
        "content": source,
        "meta": {"description": owner},
    }
    if current is None:
        result = client.post("/api/v1/functions/create", json=form)
        result.raise_for_status()
        current = result.json()
        outcome = FilterSync.INSTALLED
    elif current.get("content") != source:
        result = client.post(endpoint + "/update", json=form)
        result.raise_for_status()
        current = result.json()
        outcome = FilterSync.UPDATED
    # Always active; global only when asked (a toggle filter is offered by the models
    # that name it, never to every model).
    for field, suffix, wanted in (
        ("is_active", "/toggle", True),
        ("is_global", "/toggle/global", is_global),
    ):
        if bool(current.get(field)) != wanted:
            result = client.post(endpoint + suffix)
            result.raise_for_status()
            current = result.json()
            if outcome is FilterSync.UNCHANGED:
                outcome = FilterSync.UPDATED
    verified = client.get(endpoint)
    verified.raise_for_status()
    record = verified.json()
    if not (
        record.get("content") == source
        and record.get("type") == "filter"
        and bool(record.get("is_active"))
        and bool(record.get("is_global")) == is_global
    ):
        raise RuntimeError(f"Open WebUI did not retain the {function_id} configuration")
    return outcome


def install_image_chat_filter(client: httpx.Client) -> FilterSync:
    """Create or update only our filter, activate it globally, and read it back."""
    return _install_function(client, FILTER_ID, FILTER_NAME, filter_source(), OWNER, is_global=True)


def install_quality_filter(client: httpx.Client) -> FilterSync:
    """The "Alta qualidade" toggle: active, not global; the models that offer it name it."""
    return _install_function(
        client,
        QUALITY_FILTER_ID,
        QUALITY_FILTER_NAME,
        quality_filter_source(),
        QUALITY_OWNER,
        is_global=False,
    )


def install_art_filter(client: httpx.Client) -> FilterSync:
    """The "Arte digital" toggle: active, not global; the models that offer it name it."""
    return _install_function(
        client, ART_FILTER_ID, ART_FILTER_NAME, art_filter_source(), ART_OWNER, is_global=False
    )


def install_photo_filter(client: httpx.Client) -> FilterSync:
    """The "Fotorrealista" toggle: active, not global; the models that offer it name it."""
    return _install_function(
        client,
        PHOTO_FILTER_ID,
        PHOTO_FILTER_NAME,
        photo_filter_source(),
        PHOTO_OWNER,
        is_global=False,
    )


def refs_filter_source() -> str:
    return (resource_directory("assets") / REFS_RESOURCE).read_text(encoding="utf-8")


def install_refs_filter(client: httpx.Client) -> FilterSync:
    """The "Buscar referências" toggle: active, not global; the models that offer it name it."""
    return _install_function(
        client,
        REFS_FILTER_ID,
        REFS_FILTER_NAME,
        refs_filter_source(),
        REFS_OWNER,
        is_global=False,
    )


# --- local administrator token ------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def first_admin_id(data_dir: Path) -> str | None:
    """The first administrator's id, read from the instance's own database.

    Read-only, and only this one value; the database stays Open WebUI's.
    """
    database = data_dir / DATABASE_FILE
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "select id from user where role = 'admin' order by created_at limit 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return str(row[0]) if row and row[0] else None


def first_admin_auth(data_dir: Path) -> tuple[str, str] | None:
    """Return the first administrator and its Open WebUI 0.12 session stamp."""
    database = data_dir / DATABASE_FILE
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "select user.id, auth.session_stamp "
            "from user join auth on auth.id = user.id "
            "where user.role = 'admin' and auth.active = 1 "
            "order by user.created_at limit 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not row or not row[0] or not row[1]:
        return None
    return str(row[0]), str(row[1])


def local_admin_token(data_dir: Path, ttl: int = TOKEN_TTL_SECONDS) -> str | None:
    """A short-lived Open WebUI 0.12 automation token, or None.

    Open WebUI 0.12 binds every bearer to the account's ``session_stamp`` and
    accepts only declared token types. This token uses ``automation`` so it
    never pretends to be an interactive browser session.
    """
    secret_file = data_dir / SECRET_KEY_FILE
    if not secret_file.is_file():
        return None
    admin = first_admin_auth(data_dir)
    if admin is None:
        return None
    admin_id, session_stamp = admin
    secret = secret_file.read_text(encoding="utf-8").strip()
    if not secret:
        return None
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {
        "id": admin_id,
        "typ": "automation",
        "session_stamp": session_stamp,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def sync_image_chat_filter(
    data_dir: Path,
    base_url: str,
    *,
    client_factory: Callable[[str, str], httpx.Client] | None = None,
) -> FilterSyncReport:
    """Bring the managed instance's copy of the filter up to date. Idempotent.

    Same source installed → nothing is written. Different or absent → created
    or updated, and activated. No administrator yet → reported, not attempted.
    An unreachable or refusing instance is reported as unavailable; a foreign
    function under our id is reported as refused. Never raises.
    """
    token = local_admin_token(data_dir)
    if token is None:
        return FilterSyncReport(
            FilterSync.NO_ADMIN,
            "Open WebUI has no administrator yet; the filter is installed on a later start",
        )
    factory = client_factory or (
        lambda url, bearer: httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {bearer}"}, timeout=30.0
        )
    )
    try:
        with factory(base_url, token) as client:
            outcomes = (
                install_image_chat_filter(client),
                install_quality_filter(client),
                install_art_filter(client),
                install_photo_filter(client),
                install_refs_filter(client),
            )
        # The report says the most that happened to any of the functions.
        outcome = next(
            (
                stronger
                for stronger in (FilterSync.INSTALLED, FilterSync.UPDATED)
                if stronger in outcomes
            ),
            FilterSync.UNCHANGED,
        )
    except PermissionError as exc:
        return FilterSyncReport(FilterSync.REFUSED, str(exc))
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return FilterSyncReport(FilterSync.UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    return FilterSyncReport(outcome, f"{FILTER_ID}: {outcome.value}")


__all__ = [
    "ART_FILTER_ID",
    "ART_FILTER_NAME",
    "FILTER_ID",
    "FILTER_NAME",
    "OWNER",
    "OWNER_PREFIX",
    "PHOTO_FILTER_ID",
    "PHOTO_FILTER_NAME",
    "QUALITY_FILTER_ID",
    "QUALITY_FILTER_NAME",
    "REFS_FILTER_ID",
    "REFS_FILTER_NAME",
    "FilterSync",
    "FilterSyncReport",
    "art_filter_source",
    "filter_path",
    "filter_source",
    "first_admin_auth",
    "first_admin_id",
    "install_art_filter",
    "install_image_chat_filter",
    "install_photo_filter",
    "install_quality_filter",
    "install_refs_filter",
    "local_admin_token",
    "photo_filter_source",
    "quality_filter_source",
    "refs_filter_source",
    "sync_image_chat_filter",
]
