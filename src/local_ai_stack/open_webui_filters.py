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


def install_image_chat_filter(client: httpx.Client) -> FilterSync:
    """Create or update only our filter, activate it globally, and read it back."""
    source = filter_source()
    endpoint = f"/api/v1/functions/id/{FILTER_ID}"
    # The pinned WebUI returns 401 for a missing id as well as for bad auth.
    # List first so an authentication failure is never mistaken for absence.
    listing = client.get("/api/v1/functions/list")
    listing.raise_for_status()
    current = None
    if any(item.get("id") == FILTER_ID for item in listing.json()):
        response = client.get(endpoint)
        response.raise_for_status()
        current = response.json()
    if current and not str(current.get("meta", {}).get("description", "")).startswith(OWNER_PREFIX):
        raise PermissionError(f"function {FILTER_ID!r} exists but was not installed by the Hub")
    outcome = FilterSync.UNCHANGED
    form = {
        "id": FILTER_ID,
        "name": FILTER_NAME,
        "content": source,
        "meta": {"description": OWNER},
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
    for field, suffix in (("is_active", "/toggle"), ("is_global", "/toggle/global")):
        if not current.get(field):
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
        and record.get("is_active")
        and record.get("is_global")
    ):
        raise RuntimeError("Open WebUI did not retain the image chat filter configuration")
    return outcome


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


def local_admin_token(data_dir: Path, ttl: int = TOKEN_TTL_SECONDS) -> str | None:
    """A token the instance accepts for its first administrator, or None.

    Signed with the instance's own secret key exactly as the instance signs
    its sessions (HS256). Short-lived and never persisted.
    """
    secret_file = data_dir / SECRET_KEY_FILE
    if not secret_file.is_file():
        return None
    admin_id = first_admin_id(data_dir)
    if admin_id is None:
        return None
    secret = secret_file.read_text(encoding="utf-8").strip()
    if not secret:
        return None
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"id": admin_id, "iat": now, "exp": now + ttl, "jti": str(uuid.uuid4())}
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
            outcome = install_image_chat_filter(client)
    except PermissionError as exc:
        return FilterSyncReport(FilterSync.REFUSED, str(exc))
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return FilterSyncReport(FilterSync.UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    return FilterSyncReport(outcome, f"{FILTER_ID}: {outcome.value}")


__all__ = [
    "FILTER_ID",
    "FILTER_NAME",
    "OWNER",
    "OWNER_PREFIX",
    "FilterSync",
    "FilterSyncReport",
    "filter_path",
    "filter_source",
    "first_admin_id",
    "install_image_chat_filter",
    "local_admin_token",
    "sync_image_chat_filter",
]
