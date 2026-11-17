"""Pepito's managed Open WebUI image gallery and one-shot mask attachments.

Installed only into the hub-owned Open WebUI 0.12.0 environment. All database
access uses upstream's asynchronous ownership APIs; SQLite transactions serialize
native state across threads and server processes without sharing Python locks.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from open_webui.env import DATA_DIR
from open_webui.internal.db import get_async_db_context
from open_webui.models.chats import ChatFile, Chats
from open_webui.models.files import Files
from open_webui.storage.provider import Storage
from open_webui.utils.auth import get_verified_user
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import select

EXTENSION_VERSION = "1"
DRAFT_TTL_SECONDS = 300
MAX_MASK_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_IMAGE_SIDE = 8192
MAX_MASK_URI = len("data:image/png;base64,") + ((MAX_MASK_BYTES + 2) // 3) * 4
MAX_TEXT = 1_000_000
MAX_GROUP_ENTRIES = 256
# A picture's recipe (SPEC-308): what the filter ran to make it, kept by number so the
# owner can read its prompt and regenerate it. A painted or automatic area travels in it.
MAX_RECIPE_BYTES = 2 * MAX_MASK_URI + 1024 * 1024
MAX_PROMPT = 20_000
REGENERATE_TTL_SECONDS = 300
# The message the browser sends for an armed regeneration; the filter recognizes it.
REGENERATE_TEXT = {
    "edited": "Regerar a imagem {number} com o prompt editado",
    "same": "Regerar a imagem {number} igual",
    "seed": "Regerar a imagem {number} com a mesma semente",
    "whole": "Regerar a imagem {number} na imagem inteira",
}
VERDICTS = ("accepted", "no_change", "too_much", "wrong_source")
ORIGINS = ("upload", "generated")
# The Hub-written JSON map of Open WebUI model id -> image stack (the image filter's STACKS_ENV).
STACKS_ENV = "PEPITO_IMAGE_STACKS"
MAX_EDITORS = 64
MAX_SEED = 2**63 - 1
TEMPORARY_PREFIXES = ("temporary:", "local:")
FILE_URL = re.compile(r"^/api/v1/files/([A-Za-z0-9_-]{1,128})/content$")
router = APIRouter(prefix="/api/v1/pepito/images", tags=["pepito-images"])


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise HTTPException(400, "Invalid chat or user identifier.")
    return value


def _temporary(chat_id):
    return isinstance(chat_id, str) and any(
        chat_id.startswith(prefix) and len(chat_id) > len(prefix) for prefix in TEMPORARY_PREFIXES
    )


def _user_root(user_id):
    return (
        Path(DATA_DIR) / "pepito-native" / hashlib.sha256(_identifier(user_id).encode()).hexdigest()
    )


def _chat_path(chat_id, user_id):
    return _user_root(user_id) / (
        hashlib.sha256(_identifier(chat_id).encode()).hexdigest() + ".sqlite3"
    )


def _temporary_registered(chat_id, user_id):
    path = _chat_path(chat_id, user_id)
    if not path.is_file():
        return False
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        row = connection.execute("SELECT value FROM state WHERE key='temporary-owner'").fetchone()
        return bool(row and json.loads(row[0]) == user_id)
    finally:
        connection.close()


def _index_group(group_id, chat_id, user_id):
    root = _user_root(user_id) / "groups"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".group-", delete=False) as stream:
            temporary = stream.name
            stream.write(chat_id.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / group_id)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _resolve_group(group_id, user_id):
    try:
        return (_user_root(user_id) / "groups" / group_id).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(404, "Gallery not found.") from exc


async def _owned_chat(chat_id: str, user_id: str):
    if _temporary(chat_id):
        if await asyncio.to_thread(_temporary_registered, chat_id, user_id):
            return None
        raise HTTPException(404, "Chat not found.")
    chat = await Chats.get_chat_by_id_and_user_id(_identifier(chat_id), _identifier(user_id))
    if chat is None or chat.user_id != user_id:
        # Deliberately indistinguishable from an unknown chat, including for admins.
        raise HTTPException(404, "Chat not found.")
    return chat


@contextmanager
def _transaction(chat_id: str, user_id: str):
    root = _user_root(user_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _chat_path(chat_id, user_id)
    connection = sqlite3.connect(path, timeout=15)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS gallery (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, entries TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS image (number INTEGER PRIMARY KEY, file_id TEXT NOT NULL, message_id TEXT NOT NULL, entry TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS recipe (number INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS raw (number INTEGER PRIMARY KEY, data BLOB NOT NULL, crop INTEGER NOT NULL)"
        )
        _migrate_selection(connection)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _put_state(connection, key, value):
    connection.execute(
        "INSERT INTO state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def _get_state(connection, key):
    row = connection.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _file_id(url):
    match = FILE_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        raise HTTPException(400, "Gallery images must be original Open WebUI files.")
    return match.group(1)


async def _owned_file(chat_id, user_id, file_id):
    file = await Files.get_file_by_id_and_user_id(file_id, user_id)
    if file is None or file.user_id != user_id or not file.path:
        raise HTTPException(404, "Image not found in this chat.")
    if _temporary(chat_id):
        # _source checks both the pre-published binding and the exact registered
        # image row. Temporary chats have no upstream chat_file foreign key.
        return file
    async with get_async_db_context() as session:
        result = await session.execute(
            select(ChatFile).filter_by(chat_id=chat_id, file_id=file_id, user_id=user_id)
        )
        if result.scalars().first() is None:
            raise HTTPException(404, "Image not found in this chat.")
    return file


def _entry(entry):
    if not isinstance(entry, dict):
        raise HTTPException(400, "Invalid gallery entry.")
    number = entry.get("number")
    if type(number) is not int or number < 1 or number > 2_147_483_647:
        raise HTTPException(400, "Invalid image number.")
    _file_id(entry.get("url"))
    result = {"number": number, "url": entry["url"]}
    for key in ("description", "style", "aspect"):
        value = entry.get(key, "")
        if not isinstance(value, str) or len(value) > 4000:
            raise HTTPException(400, "Invalid gallery description.")
        result[key] = value
    for key in ("group", "variation", "total", "current"):
        value = entry.get(key)
        if value is not None and (
            isinstance(value, (dict, list))
            or not isinstance(value, (str, int, bool))
            or len(str(value)) > 256
        ):
            raise HTTPException(400, "Invalid gallery metadata.")
        result[key] = value
    origin = entry.get("origin")
    if origin is not None and origin not in ORIGINS:
        raise HTTPException(400, "Invalid gallery image origin.")
    result["origin"] = origin
    attempt_id = entry.get("attempt_id")
    if attempt_id is not None and (
        not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 256
    ):
        raise HTTPException(400, "Invalid gallery attempt identifier.")
    result["attempt_id"] = attempt_id
    return result


async def publish_gallery(chat_id: str, user_id: str, message_id: str, entries: list[dict]) -> str:
    """Register trusted filter outputs, linking owned files before publishing a marker."""
    _identifier(chat_id)
    _identifier(user_id)
    if not _temporary(chat_id):
        await _owned_chat(chat_id, user_id)
    _identifier(message_id)
    if not isinstance(entries, list) or not entries or len(entries) > MAX_GROUP_ENTRIES:
        raise HTTPException(400, "A gallery must contain between 1 and 256 images.")
    clean = [_entry(entry) for entry in entries]
    if len({entry["number"] for entry in clean}) != len(clean):
        raise HTTPException(400, "Duplicate gallery image numbers.")
    ids = [_file_id(entry["url"]) for entry in clean]
    for file_id in ids:
        file = await Files.get_file_by_id_and_user_id(file_id, user_id)
        if file is None or file.user_id != user_id or not file.path:
            raise HTTPException(404, "Image not found.")
    # This helper is internal (there is no publish route). Files newly generated by
    # the filter need their upstream chat association before the browser saves history.
    if not _temporary(chat_id):
        await Chats.insert_chat_files(chat_id, message_id, ids, user_id)
        for file_id in ids:
            await _owned_file(chat_id, user_id, file_id)
    group_id = uuid.uuid4().hex

    def save():
        with _transaction(chat_id, user_id) as connection:
            for entry, file_id in zip(clean, ids):
                old = connection.execute(
                    "SELECT file_id,entry FROM image WHERE number=?", (entry["number"],)
                ).fetchone()
                if old and old[0] != file_id:
                    raise HTTPException(
                        409, "An image number cannot be reassigned to another source."
                    )
                # An acceptance is bound to the attempt that made this exact number.
                bound = json.loads(old[1]).get("attempt_id") if old else None
                if bound is not None and entry["attempt_id"] is None:
                    entry["attempt_id"] = bound
                elif bound is not None and bound != entry["attempt_id"]:
                    raise HTTPException(409, "An image cannot be rebound to another attempt.")
                # Preserve the original membership: upstream only links a file once
                # per chat, even when it appears in several gallery groups.
                connection.execute(
                    "INSERT INTO image(number,file_id,message_id,entry) VALUES (?,?,?,?) ON CONFLICT(number) DO UPDATE SET entry=excluded.entry",
                    (entry["number"], file_id, message_id, json.dumps(entry)),
                )
            connection.execute(
                "INSERT INTO gallery(id,message_id,entries) VALUES (?,?,?)",
                (group_id, message_id, json.dumps(clean)),
            )
            # A pick applies until the next delivered batch. Publishing advances the
            # default source, without accepting it or pinning edits to an older pick.
            _put_state(connection, "current", clean[-1]["number"])
            connection.execute("DELETE FROM state WHERE key='selection'")
            if _temporary(chat_id):
                _put_state(connection, "temporary-owner", user_id)
        _index_group(group_id, chat_id, user_id)
        return group_id

    return await asyncio.to_thread(save)


def _lookup_image(chat_id, user_id, number):
    with _transaction(chat_id, user_id) as connection:
        return connection.execute(
            "SELECT file_id,message_id,entry FROM image WHERE number=?", (number,)
        ).fetchone()


async def _source(chat_id, user_id, number):
    await _owned_chat(chat_id, user_id)
    record = await asyncio.to_thread(_lookup_image, chat_id, user_id, number)
    if record is None:
        raise HTTPException(404, "Image not found in this chat.")
    file = await _owned_file(chat_id, user_id, record[0])
    return file


def _read_source(file):
    local = Path(Storage.get_file(file.path))
    if local.stat().st_size > MAX_SOURCE_BYTES:
        raise HTTPException(413, "Source image is too large.")
    with local.open("rb") as stream:
        data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(413, "Source image is too large.")
    return data


def _open_image(data, *, mask=False):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if (
                    width < 1
                    or height < 1
                    or max(width, height) > MAX_IMAGE_SIDE
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise HTTPException(400, "Image dimensions exceed the editor limit.")
                if mask and (image.format != "PNG" or getattr(image, "n_frames", 1) != 1):
                    raise HTTPException(400, "The mask must be a single PNG image.")
                if not mask and image.format not in ("PNG", "JPEG", "WEBP"):
                    raise HTTPException(400, "Unsupported source image type.")
                image.load()
                if mask:
                    rgba = image.convert("RGBA")
                    red, green, blue, alpha = rgba.split()
                    if (
                        alpha.getextrema() != (255, 255)
                        or red.tobytes() != green.tobytes()
                        or red.tobytes() != blue.tobytes()
                    ):
                        raise HTTPException(
                            400,
                            "The mask must be opaque grayscale (white selected, black unselected).",
                        )
                    if red.getextrema()[1] == 0:
                        raise HTTPException(400, "Select an area before sending the mask.")
                return image.size, image.format
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise HTTPException(400, "Invalid or oversized image payload.") from exc


def _decode_mask(uri):
    if (
        not isinstance(uri, str)
        or len(uri) > MAX_MASK_URI
        or not uri.startswith("data:image/png;base64,")
    ):
        raise HTTPException(400, "The mask must be a bounded PNG data URI.")
    try:
        data = base64.b64decode(uri.partition(",")[2], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(400, "Invalid PNG mask encoding.") from exc
    if not data or len(data) > MAX_MASK_BYTES:
        raise HTTPException(413, "The PNG mask is too large.")
    _open_image(data, mask=True)
    return data


def _validate_dimensions(mask, source):
    mask_size, _ = _open_image(mask, mask=True)
    source_size, _ = _open_image(source)
    if mask_size != source_size:
        raise HTTPException(400, "The mask dimensions must match the original image.")


async def read_selection(chat_id: str, user_id: str) -> int | None:
    """The user's explicit selection only; the last published image is not one."""
    if _temporary(chat_id) and not await asyncio.to_thread(_temporary_registered, chat_id, user_id):
        return None
    await _owned_chat(chat_id, user_id)

    def read():
        with _transaction(chat_id, user_id) as connection:
            return _get_state(connection, "selection")

    number = await asyncio.to_thread(read)
    if number is not None:
        await _source(chat_id, user_id, number)
    return number


# Notes the user writes to the developer, not to the model: ``['...']`` or ``[Ignorar: ...]``.
# The same pattern as ``_IGNORED`` in the filter (``pepito_image_chat.py``), which takes them
# out of the message before it reaches ``consume_draft``; the page arms the draft with them in.
_IGNORED = re.compile(
    r"\[\s*['\u2018\u2019][^\[\]]*['\u2018\u2019]\s*\]"
    r"|\[\s*ign[o\u00f2\u00f3\u00f4\u00f5\u00f6]rar\s*:[^\[\]]*\]",
    re.IGNORECASE,
)


def _text_hash(text: str) -> str:
    """The words a draft is armed for, without the ignored notes and whatever spacing the
    client sends them with. The one normalization of both sides: arming (the page's text)
    and consuming (the filter's)."""
    words = _IGNORED.sub(" ", text).split()
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()


async def consume_draft(chat_id: str, user_id: str, text: str) -> dict | None:
    """The area painted for exactly these words, used once.

    None: no draft is armed. ``{"number", "png"}``: the draft matched and is used up.
    ``{"number", "issue", "selected"}``: a draft is armed but cannot be used on this
    message; the caller says why instead of editing without it. A draft armed for other
    words (``issue`` "text") is reported once and removed: the page arms a new one with
    every message sent with an area, so a leftover never blocks the next message. One for a
    picture that is no longer the selected one ("source") stays armed until it is selected
    again; an expired draft ("expired") or one whose picture no longer matches it
    ("invalid") is removed. Of two concurrent requests for the same draft, one gets it."""
    if _temporary(chat_id) and not await asyncio.to_thread(_temporary_registered, chat_id, user_id):
        return None
    await _owned_chat(chat_id, user_id)

    def read():
        with _transaction(chat_id, user_id) as connection:
            return _get_state(connection, "draft"), _effective_selection(connection)

    def remove(draft_id):
        with _transaction(chat_id, user_id) as connection:
            current = _get_state(connection, "draft")
            if current is None or current["draft_id"] != draft_id:
                return False
            connection.execute("DELETE FROM state WHERE key='draft'")
            return True

    draft, selected = await asyncio.to_thread(read)
    if draft is None:
        return None
    issue = {"number": draft["number"], "selected": selected}
    if draft["expires_at"] <= time.time():
        await asyncio.to_thread(remove, draft["draft_id"])
        return {**issue, "issue": "expired"}
    if not isinstance(text, str) or draft["text_hash"] != _text_hash(text):
        await asyncio.to_thread(remove, draft["draft_id"])
        return {**issue, "issue": "text"}
    if selected != draft["number"]:
        return {**issue, "issue": "source"}
    try:
        file = await _source(chat_id, user_id, draft["number"])
        data = _decode_mask(draft["mask_png"])
        source = await asyncio.to_thread(_read_source, file)
        await asyncio.to_thread(_validate_dimensions, data, source)
    except HTTPException:
        await asyncio.to_thread(remove, draft["draft_id"])
        return {**issue, "issue": "invalid"}
    if not await asyncio.to_thread(remove, draft["draft_id"]):
        return None  # another request used it first
    return {"number": draft["number"], "png": data}


# --- the recipe of each delivered picture, and a regeneration armed from it (SPEC-308) ---
#
# The filter records what it ran for each picture it delivers (``record_recipes``). Its
# owner reads it (``GET /recipe/{number}``) and may arm a regeneration of that recipe with
# the prompt edited, or as it was with a new seed (``POST /regenerate``): like a painted
# area, it is armed for exactly one fixed message and expires; the filter uses it up
# (``consume_regeneration``) and makes the picture without the router.


def _recipe_bytes(recipe) -> bytes | None:
    try:
        data = json.dumps(recipe, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return data if len(data) <= MAX_RECIPE_BYTES else None


async def record_recipes(
    chat_id: str, user_id: str, recipes: dict, raws: dict | None = None
) -> int:
    """Keep the recipe of each published picture by its number (internal: no route).

    Only numbers this chat already registered take a recipe; an oversized one is skipped.
    ``raws`` (SPEC-318) maps a number to ``(bytes, cropped)``: what the model drew before the
    area was pasted on the source, kept beside the recipe unless over ``MAX_SOURCE_BYTES``.
    Returns how many recipes were kept."""
    if _temporary(chat_id):
        if not await asyncio.to_thread(_temporary_registered, chat_id, user_id):
            return 0
    else:
        await _owned_chat(chat_id, user_id)
    clean = {}
    for number, recipe in (recipes or {}).items():
        data = _recipe_bytes(recipe) if isinstance(recipe, dict) else None
        if type(number) is int and data is not None:
            clean[number] = data.decode("utf-8")
    kept_raws = {
        number: (bytes(value[0]), 1 if value[1] else 0)
        for number, value in (raws or {}).items()
        if number in clean and value[0] and len(value[0]) <= MAX_SOURCE_BYTES
    }

    def save():
        kept = 0
        with _transaction(chat_id, user_id) as connection:
            for number, value in clean.items():
                if connection.execute("SELECT 1 FROM image WHERE number=?", (number,)).fetchone():
                    connection.execute(
                        "INSERT INTO recipe(number,value) VALUES (?,?) ON CONFLICT(number) DO UPDATE SET value=excluded.value",
                        (number, value),
                    )
                    connection.execute("DELETE FROM raw WHERE number=?", (number,))
                    if number in kept_raws:
                        connection.execute(
                            "INSERT INTO raw(number,data,crop) VALUES (?,?,?)",
                            (number, *kept_raws[number]),
                        )
                    kept += 1
        return kept

    return await asyncio.to_thread(save)


def _read_recipe(chat_id, user_id, number):
    with _transaction(chat_id, user_id) as connection:
        row = connection.execute("SELECT value FROM recipe WHERE number=?", (number,)).fetchone()
    return json.loads(row[0]) if row else None


async def _owned_recipe(chat_id: str, user_id: str, number: int) -> dict:
    """The recipe of a picture of this user's chat; the same 404 as an unknown picture."""
    await _source(chat_id, user_id, number)
    recipe = await asyncio.to_thread(_read_recipe, chat_id, user_id, number)
    if not isinstance(recipe, dict):
        raise HTTPException(404, "Esta imagem não tem receita registrada.")
    return recipe


def _raw_crop(chat_id, user_id, number):
    """Whether the kept raw output is only the crop, or None when none is kept."""
    with _transaction(chat_id, user_id) as connection:
        row = connection.execute("SELECT crop FROM raw WHERE number=?", (number,)).fetchone()
    return None if row is None else bool(row[0])


def _read_raw(chat_id, user_id, number):
    with _transaction(chat_id, user_id) as connection:
        return connection.execute("SELECT data,crop FROM raw WHERE number=?", (number,)).fetchone()


async def read_raw_output(chat_id: str, user_id: str, number: int) -> tuple[bytes, bool]:
    """What the model drew for an edit with an area, before the paste (SPEC-318), and whether
    it is only the crop; only its owner, with the same 404 as an unknown picture."""
    if type(number) is not int or number < 1 or number > 2_147_483_647:
        raise HTTPException(400, "Invalid image number.")
    await _owned_recipe(chat_id, user_id, number)
    row = await asyncio.to_thread(_read_raw, chat_id, user_id, number)
    if row is None:
        raise HTTPException(404, "A saída do modelo não foi guardada.")
    return bytes(row[0]), bool(row[1])


def recipe_steps(recipe: dict) -> dict:
    """The prompts a user may edit, by step: the one prompt of a generation or an edit, or
    each prompt of a swap (its character reference and the variation's own generation)."""
    prompts = recipe.get("prompts")
    if isinstance(prompts, dict) and prompts:
        return {key: value for key, value in prompts.items() if isinstance(value, str)}
    prompt = recipe.get("prompt")
    return {"prompt": prompt} if isinstance(prompt, str) else {}


def recipe_regenerable(recipe: dict) -> bool:
    """Whether its sources are still Open WebUI files (a web or inline source is not kept)."""
    if recipe.get("kind") not in ("generate", "edit", "swap") or not recipe_steps(recipe):
        return False
    if recipe["kind"] == "generate":
        return True
    urls = recipe.get("source_urls")
    return (
        isinstance(urls, list)
        and bool(urls)
        and all(isinstance(url, str) and FILE_URL.fullmatch(url) for url in urls)
    )


def recipe_seed(recipe: dict) -> int | None:
    """The seed that remakes the picture as it was, or None when the recipe kept none: an
    edit's is the one its patch carried (what the workflow was given), a new picture's and a
    swap's (the turn's) is the recipe's own."""
    seed = (
        (recipe.get("patch") if isinstance(recipe.get("patch"), dict) else {}).get("seed")
        if recipe.get("kind") == "edit"
        else recipe.get("seed")
    )
    return seed if type(seed) is int and 0 <= seed <= MAX_SEED else None


def _canvas(recipe: dict) -> dict | None:
    patch = recipe.get("patch") if isinstance(recipe.get("patch"), dict) else {}
    width, height = patch.get("width"), patch.get("height")
    if not (type(width) is int and type(height) is int):
        match = re.fullmatch(r"(\d{1,5})x(\d{1,5})", str(recipe.get("size") or ""))
        width, height = (int(match.group(1)), int(match.group(2))) if match else (None, None)
    return {"width": width, "height": height} if width and height else None


def recipe_view(number: int, recipe: dict, raw_crop: bool | None = None) -> dict:
    """What the owner sees: the prompt(s), the part, the sources, the area and the seed."""
    mask = recipe.get("mask_png")
    steps = recipe_steps(recipe)
    step = recipe.get("method") if recipe.get("method") in steps else next(iter(steps), None)
    return {
        "number": number,
        "kind": recipe.get("kind"),
        "part": recipe.get("part"),
        "edit_mode": recipe.get("edit_mode"),
        "prompt": steps.get(step, ""),
        "step": step,
        "steps": [{"key": key, "prompt": value} for key, value in steps.items()],
        "method": recipe.get("method"),
        "sources": [n for n in recipe.get("sources") or [] if type(n) is int],
        "area_png": "data:image/png;base64," + mask if isinstance(mask, str) and mask else None,
        "canvas": _canvas(recipe),
        "seed": recipe_seed(recipe),
        "style": recipe.get("style"),
        "aspect": recipe.get("aspect"),
        "model": recipe.get("model"),
        "regenerable": recipe_regenerable(recipe),
        "raw_available": raw_crop is not None,
        "raw_crop": bool(raw_crop),
    }


async def read_recipe(chat_id: str, user_id: str, number: int) -> dict:
    if type(number) is not int or number < 1 or number > 2_147_483_647:
        raise HTTPException(400, "Invalid image number.")
    recipe = await _owned_recipe(chat_id, user_id, number)
    raw_crop = await asyncio.to_thread(_raw_crop, chat_id, user_id, number)
    return {"chat_id": chat_id, **recipe_view(number, recipe, raw_crop)}


async def arm_regeneration(
    chat_id: str, user_id: str, number: int, mode: str, prompt: str | None, step: str | None
) -> dict:
    """Arm one regeneration of picture ``number``'s recipe for its fixed message."""
    if mode not in REGENERATE_TEXT:
        raise HTTPException(400, "Invalid regeneration mode.")
    recipe = await _owned_recipe(chat_id, user_id, number)
    if not recipe_regenerable(recipe):
        raise HTTPException(
            409, "A receita desta imagem não pode ser refeita: uma origem dela não está no chat."
        )
    steps = recipe_steps(recipe)
    if step is None:
        step = recipe.get("method") if recipe.get("method") in steps else next(iter(steps))
    if step not in steps:
        raise HTTPException(400, "Esta etapa não existe na receita da imagem.")
    if mode == "edited":
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(400, "Escreva o prompt antes de regerar.")
        prompt = prompt.strip()
    elif mode == "seed":
        if recipe_seed(recipe) is None:
            raise HTTPException(
                409, "Esta imagem não guardou a semente; use Regerar (nova semente)."
            )
        prompt = None
    elif mode == "whole":
        if (
            recipe.get("kind") != "edit"
            or not recipe.get("mask_png")
            or str((recipe.get("recipe") or {}).get("edit_method") or "").startswith("warp")
        ):
            raise HTTPException(409, "Só imagens editadas numa área podem ser refeitas inteiras.")
        prompt = None
    else:
        prompt = None
    text = REGENERATE_TEXT[mode].format(number=number)
    expires_at = time.time() + REGENERATE_TTL_SECONDS
    armed = {
        "regen_id": uuid.uuid4().hex,
        "number": number,
        "mode": mode,
        "step": step,
        "prompt": prompt,
        "text_hash": _text_hash(text),
        "expires_at": expires_at,
    }

    def save():
        with _transaction(chat_id, user_id) as connection:
            _put_state(connection, "regenerate", armed)

    await asyncio.to_thread(save)
    return {
        "regen_id": armed["regen_id"],
        "number": number,
        "mode": mode,
        "step": step,
        "text": text,
        "expires_at": expires_at,
    }


async def consume_regeneration(chat_id: str, user_id: str, text: str) -> dict | None:
    """The regeneration armed for exactly these words, used once.

    None: nothing is armed. ``{"number", "mode", "step", "prompt", "recipe"}``: it matched and
    is used up (``prompt`` is None for "same": the recipe's own). ``{"number", "issue"}``: one is
    armed but cannot be used: "text" (armed for another message; kept for it), "expired" or
    "invalid" (its picture or recipe is gone; both removed). Of two concurrent requests, one
    gets it."""
    if _temporary(chat_id) and not await asyncio.to_thread(_temporary_registered, chat_id, user_id):
        return None
    await _owned_chat(chat_id, user_id)

    def read():
        with _transaction(chat_id, user_id) as connection:
            return _get_state(connection, "regenerate")

    def remove(regen_id):
        with _transaction(chat_id, user_id) as connection:
            current = _get_state(connection, "regenerate")
            if current is None or current["regen_id"] != regen_id:
                return False
            connection.execute("DELETE FROM state WHERE key='regenerate'")
            return True

    armed = await asyncio.to_thread(read)
    if armed is None:
        return None
    issue = {"number": armed["number"], "mode": armed["mode"]}
    if armed["expires_at"] <= time.time():
        await asyncio.to_thread(remove, armed["regen_id"])
        return {**issue, "issue": "expired"}
    if not isinstance(text, str) or armed["text_hash"] != _text_hash(text):
        return {**issue, "issue": "text"}
    try:
        recipe = await _owned_recipe(chat_id, user_id, armed["number"])
    except HTTPException:
        recipe = None
    if (
        recipe is None
        or not recipe_regenerable(recipe)
        or armed["step"] not in recipe_steps(recipe)
    ):
        await asyncio.to_thread(remove, armed["regen_id"])
        return {**issue, "issue": "invalid"}
    if not await asyncio.to_thread(remove, armed["regen_id"]):
        return None  # another request used it first
    return {
        "number": armed["number"],
        "mode": armed["mode"],
        "step": armed["step"],
        "prompt": armed["prompt"],
        "recipe": recipe,
    }


class RegenerateForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=256)
    number: StrictInt = Field(ge=1, le=2_147_483_647)
    mode: Literal["edited", "same", "seed", "whole"]
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT)
    step: str | None = Field(default=None, min_length=1, max_length=64)


class SelectionForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=256)
    number: StrictInt = Field(ge=1, le=2_147_483_647)


class DraftForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=256)
    source_number: StrictInt = Field(ge=1, le=2_147_483_647)
    mask_png: str = Field(max_length=MAX_MASK_URI)
    text: str = Field(max_length=MAX_TEXT)


@router.get("/gallery/{group_id}")
async def get_gallery(group_id: str, chat_id: str | None = None, user=Depends(get_verified_user)):
    if not re.fullmatch(r"[a-f0-9]{32}", group_id):
        raise HTTPException(404, "Gallery not found.")
    registered_chat = await asyncio.to_thread(_resolve_group, group_id, user.id)
    if chat_id is not None and chat_id != registered_chat:
        raise HTTPException(404, "Gallery not found.")
    chat_id = registered_chat
    await _owned_chat(chat_id, user.id)

    def read():
        with _transaction(chat_id, user.id) as connection:
            row = connection.execute(
                "SELECT message_id,entries FROM gallery WHERE id=?", (group_id,)
            ).fetchone()
            return (row, _numbers(connection), _get_state(connection, f"preferred:{group_id}"))

    row, numbers, preferred = await asyncio.to_thread(read)
    if row is None:
        raise HTTPException(404, "Gallery not found.")
    entries = json.loads(row[1])
    for entry in entries:
        await _source(chat_id, user.id, entry["number"])
    return {
        "group_id": group_id,
        "chat_id": chat_id,
        "message_id": row[0],
        **numbers,
        "preferred_number": preferred if type(preferred) is int else None,
        "entries": entries,
    }


async def select_image(chat_id: str, user_id: str, number: int) -> int:
    """Persist a UI or typed source selection, without submitting a prompt."""
    if type(number) is not int or number < 1 or number > 2_147_483_647:
        raise HTTPException(400, "Invalid image number.")
    await _source(chat_id, user_id, number)

    def save():
        with _transaction(chat_id, user_id) as connection:
            _put_state(connection, "selection", number)

    await asyncio.to_thread(save)
    return number


@router.post("/selection")
async def post_selection(form: SelectionForm, user=Depends(get_verified_user)):
    await select_image(form.chat_id, user.id, form.number)
    return {"chat_id": form.chat_id, "selected_number": form.number}


class PreferenceForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=256)
    number: StrictInt = Field(ge=1, le=2_147_483_647)


@router.post("/preference")
async def post_preference(form: PreferenceForm, user=Depends(get_verified_user)):
    """SPEC-319: "Prefiro esta" in a gallery of 2+ pictures. The choice is kept per gallery
    (the last click wins) and selects the picture as the base of the next edits."""
    await _source(form.chat_id, user.id, form.number)

    def save():
        with _transaction(form.chat_id, user.id) as connection:
            for group_id, raw in connection.execute(
                "SELECT id,entries FROM gallery ORDER BY rowid DESC"
            ).fetchall():
                entries = json.loads(raw)
                if len(entries) >= 2 and any(item.get("number") == form.number for item in entries):
                    _put_state(connection, f"preferred:{group_id}", form.number)
                    return group_id
        return None

    group_id = await asyncio.to_thread(save)
    if group_id is None:
        raise HTTPException(409, "The image is not in a gallery with two or more images.")
    await select_image(form.chat_id, user.id, form.number)
    return {"group_id": group_id, "preferred_number": form.number, "selected_number": form.number}


# A numbered picture never changes bytes: publish_gallery refuses to rebind a number
# to another file. Persistent chats may therefore be cached long by the owner's
# browser. Temporary ids can be recycled by a later session, so those revalidate.
SOURCE_CACHE_PERSISTENT = "private, max-age=31536000, immutable"
SOURCE_CACHE_TEMPORARY = "private, no-cache"


def _etag_matches(header: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match uses weak comparison: W/ prefixes are ignored."""
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


@router.get("/source/{number}")
async def get_source(
    request: Request,
    number: int,
    chat_id: str,
    v: str | None = None,
    user=Depends(get_verified_user),
):
    # Ownership is always checked first: a validator never bypasses authorization,
    # and another user's If-None-Match gets the same 404 as an unknown picture.
    file = await _source(chat_id, user.id, number)
    if v is not None and v != file.id:
        # The client keys its cache by the file id it saw in the gallery entry.
        raise HTTPException(404, "Image not found in this chat.")
    data = await asyncio.to_thread(_read_source, file)
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    headers = {
        "Cache-Control": SOURCE_CACHE_TEMPORARY if _temporary(chat_id) else SOURCE_CACHE_PERSISTENT,
        "ETag": etag,
        # Never let a cache serve one account's picture to another session.
        "Vary": "Authorization, Cookie",
        "X-Content-Type-Options": "nosniff",
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    _, kind = await asyncio.to_thread(_open_image, data)
    return Response(
        content=data,
        media_type={"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[kind],
        headers=headers,
    )


@router.post("/draft")
async def arm_draft(request: Request, user=Depends(get_verified_user)):
    # Bound bytes while streaming, before JSON parsing allocates a large payload.
    limit = MAX_MASK_URI + MAX_TEXT * 6 + 4096
    length = request.headers.get("content-length")
    if length and (not length.isdecimal() or int(length) > limit):
        raise HTTPException(413, "Mask attachment request is too large.")
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > limit:
            raise HTTPException(413, "Mask attachment request is too large.")
        payload.extend(chunk)
    try:
        form = DraftForm.model_validate_json(payload)
    except ValueError as exc:
        raise HTTPException(422, "Invalid mask attachment request.") from exc
    file = await _source(form.chat_id, user.id, form.source_number)
    data = await asyncio.to_thread(_decode_mask, form.mask_png)
    source = await asyncio.to_thread(_read_source, file)
    await asyncio.to_thread(_validate_dimensions, data, source)
    expires_at = time.time() + DRAFT_TTL_SECONDS
    draft_id = uuid.uuid4().hex
    draft = {
        "draft_id": draft_id,
        "number": form.source_number,
        "mask_png": form.mask_png,
        "text_hash": _text_hash(form.text),
        "expires_at": expires_at,
    }

    def save():
        with _transaction(form.chat_id, user.id) as connection:
            if _effective_selection(connection) != form.source_number:
                raise HTTPException(
                    409,
                    "The selected image does not match this mask. Select its original image again.",
                )
            _put_state(connection, "draft", draft)

    await asyncio.to_thread(save)
    return {"draft_id": draft_id, "source_number": form.source_number, "expires_at": expires_at}


@router.delete("/draft")
async def delete_draft(chat_id: str, draft_id: str | None = None, user=Depends(get_verified_user)):
    await _owned_chat(chat_id, user.id)

    def delete():
        with _transaction(chat_id, user.id) as connection:
            current = _get_state(connection, "draft")
            if current is None or (draft_id is not None and current["draft_id"] != draft_id):
                return False
            connection.execute("DELETE FROM state WHERE key='draft'")
            return True

    deleted = await asyncio.to_thread(delete)
    return {"deleted": deleted}


@router.get("/recipe/{number}")
async def get_recipe(number: int, chat_id: str, user=Depends(get_verified_user)):
    return await read_recipe(chat_id, user.id, number)


@router.get("/raw/{number}")
async def get_raw(number: int, chat_id: str, user=Depends(get_verified_user)):
    data, cropped = await read_raw_output(chat_id, user.id, number)
    _, kind = await asyncio.to_thread(_open_image, data)
    return Response(
        content=data,
        media_type={"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[kind],
        headers={
            "Cache-Control": "private, no-store",
            "Vary": "Authorization, Cookie",
            "X-Content-Type-Options": "nosniff",
            "X-Pepito-Raw-Crop": "1" if cropped else "0",
        },
    )


@router.post("/regenerate")
async def post_regenerate(form: RegenerateForm, user=Depends(get_verified_user)):
    return await arm_regeneration(
        form.chat_id, user.id, form.number, form.mode, form.prompt, form.step
    )


@router.delete("/regenerate")
async def delete_regenerate(
    chat_id: str, regen_id: str | None = None, user=Depends(get_verified_user)
):
    await _owned_chat(chat_id, user.id)

    def delete():
        with _transaction(chat_id, user.id) as connection:
            current = _get_state(connection, "regenerate")
            if current is None or (regen_id is not None and current["regen_id"] != regen_id):
                return False
            connection.execute("DELETE FROM state WHERE key='regenerate'")
            return True

    return {"deleted": await asyncio.to_thread(delete)}


def _migrate_selection(connection):
    """Once per chat: before explicit/current were split, publishing wrote ``selection``.

    A stored selection equal to the newest image is that automatic default and becomes
    ``current``; anything else was a deliberate pick and stays the explicit selection.
    """
    if _get_state(connection, "selection-migrated"):
        return
    latest = connection.execute("SELECT MAX(number) FROM image").fetchone()[0]
    if latest is not None:
        _put_state(connection, "current", latest)
        if _get_state(connection, "selection") == latest:
            connection.execute("DELETE FROM state WHERE key='selection'")
    _put_state(connection, "selection-migrated", True)


def _effective_selection(connection):
    """What the editor shows as its source: the explicit pick, else the newest image."""
    explicit = _get_state(connection, "selection")
    return explicit if explicit is not None else _get_state(connection, "current")


def _numbers(connection):
    explicit = _get_state(connection, "selection")
    current = _get_state(connection, "current")
    return {
        "explicit_selected_number": explicit,
        "accepted_number": _get_state(connection, "accepted"),
        "current_number": current,
        # Display only: neither the accepted image nor the anchor is implied by it.
        "selected_number": explicit if explicit is not None else current,
        "selection_source": "explicit"
        if explicit is not None
        else ("current" if current is not None else None),
    }


async def read_state(chat_id: str, user_id: str) -> dict:
    """Explicit, accepted and current image numbers of an owned chat, kept separate."""
    await _owned_chat(chat_id, user_id)

    def read():
        with _transaction(chat_id, user_id) as connection:
            return _numbers(connection)

    return await asyncio.to_thread(read)


def _calibration():
    try:
        from open_webui import pepito_calibration
    except ImportError as exc:
        raise HTTPException(503, "Image calibration is not installed.") from exc
    return pepito_calibration


async def approve_image(chat_id: str, user_id: str, number: int, verdict: str = "accepted") -> dict:
    """Record the user's verdict on one delivered image; the single path for UI and typed text.

    The image must belong to this user's chat and to the attempt registered for exactly this
    image. Accepting never selects the image, and selecting never accepts it.
    """
    if verdict not in VERDICTS:
        raise HTTPException(400, "Invalid verdict.")
    if type(number) is not int or number < 1 or number > 2_147_483_647:
        raise HTTPException(400, "Invalid image number.")
    await _source(chat_id, user_id, number)
    record = await asyncio.to_thread(_lookup_image, chat_id, user_id, number)
    attempt_id = json.loads(record[2]).get("attempt_id")
    if not attempt_id:
        raise HTTPException(409, "This image has no registered generation attempt.")
    calibration = _calibration()
    data_dir = str(DATA_DIR)
    attempt = await asyncio.to_thread(
        calibration.attempt_state, data_dir, user_id, chat_id, attempt_id
    )
    if attempt is None or attempt.get("output_number") != number:
        raise HTTPException(409, "This image does not match a registered generation attempt.")
    try:
        feedback = await asyncio.to_thread(
            calibration.record_feedback, data_dir, user_id, chat_id, attempt_id, verdict
        )
    except ValueError as exc:
        raise HTTPException(409, "This image's attempt cannot take feedback.") from exc

    def save():
        with _transaction(chat_id, user_id) as connection:
            if verdict == "accepted":
                _put_state(connection, "accepted", number)
            elif _get_state(connection, "accepted") == number:
                connection.execute("DELETE FROM state WHERE key='accepted'")
            return _numbers(connection)

    numbers = await asyncio.to_thread(save)
    return {"chat_id": chat_id, "number": number, **numbers, "feedback": feedback}


def _editors():
    """Edit-capable stacks the Hub configured, only as the Hub wrote them."""
    path = os.environ.get(STACKS_ENV, "").strip()
    if not path:
        return []
    try:
        stacks = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(stacks, dict):
        return []
    editors = []
    for model_id, stack in stacks.items():
        edit = stack.get("edit") if isinstance(stack, dict) else None
        if (
            not isinstance(model_id, str)
            or not model_id
            or len(model_id) > 256
            or not isinstance(edit, dict)
            or not edit.get("workflow")
        ):
            continue
        label = stack.get("label")
        editor = {
            "model_id": model_id,
            "label": label[:200] if isinstance(label, str) and label.strip() else model_id,
        }
        family = stack.get("family")
        if isinstance(family, str) and family:
            editor["family"] = family[:64]
        editors.append(editor)
        if len(editors) >= MAX_EDITORS:
            break
    return editors


async def read_preferences(chat_id: str, user_id: str) -> dict:
    """Per-chat image settings: area grounding, local shape method, and generic editor.

    ``editor_model_id`` is only what the user chose (None: the chat's own stack); it is never
    replaced silently, and ``editor_available`` says whether the Hub still configures it.
    ``edit_method`` defaults to auto independently of installed editors; the image filter
    checks method dependencies when executing a local shape edit, without silent fallback.
    """
    if _temporary(chat_id) and not await asyncio.to_thread(_temporary_registered, chat_id, user_id):
        return {
            "chat_id": chat_id,
            "auto_region": True,
            "editor_model_id": None,
            "edit_method": "auto",
            "editor_available": True,
            "comparison_seed": None,
            "editors": await asyncio.to_thread(_editors),
        }
    await _owned_chat(chat_id, user_id)

    def read():
        with _transaction(chat_id, user_id) as connection:
            return _get_state(connection, "preferences") or {}

    saved = await asyncio.to_thread(read)
    editors = await asyncio.to_thread(_editors)
    editor = saved.get("editor_model_id")
    return {
        "chat_id": chat_id,
        "auto_region": saved.get("auto_region", True),
        "editor_model_id": editor,
        "edit_method": saved.get("edit_method", "auto"),
        "editor_available": editor is None or any(item["model_id"] == editor for item in editors),
        "comparison_seed": saved.get("comparison_seed"),
        "editors": editors,
    }


async def write_preferences(chat_id: str, user_id: str, changes: dict) -> dict:
    """Validate and persist only the given preference fields, then return all of them."""
    await _owned_chat(chat_id, user_id)
    unknown = set(changes) - {"auto_region", "editor_model_id", "comparison_seed", "edit_method"}
    if unknown:
        raise HTTPException(400, "Unknown image preference.")
    if "auto_region" in changes and type(changes["auto_region"]) is not bool:
        raise HTTPException(400, "auto_region must be a boolean.")
    if "edit_method" in changes and changes["edit_method"] not in (
        "auto",
        "qwen",
        "warp",
        "warp_qwen",
    ):
        raise HTTPException(400, "edit_method must be auto, qwen, warp, or warp_qwen.")
    seed = changes.get("comparison_seed")
    if (
        "comparison_seed" in changes
        and seed is not None
        and (type(seed) is not int or seed < 0 or seed > MAX_SEED)
    ):
        raise HTTPException(400, "comparison_seed must be a nonnegative 63-bit integer.")
    editor = changes.get("editor_model_id")
    if editor is not None:
        editors = await asyncio.to_thread(_editors)
        if not isinstance(editor, str) or all(item["model_id"] != editor for item in editors):
            raise HTTPException(400, "That editor is not configured for image editing.")

    def save():
        with _transaction(chat_id, user_id) as connection:
            current = _get_state(connection, "preferences") or {}
            _put_state(connection, "preferences", {**current, **changes})

    await asyncio.to_thread(save)
    return await read_preferences(chat_id, user_id)


class AcceptanceForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=256)
    number: StrictInt = Field(ge=1, le=2_147_483_647)
    verdict: Literal["accepted", "no_change", "too_much", "wrong_source"]


class PreferencesForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=256)
    auto_region: StrictBool = True
    editor_model_id: str | None = Field(default=None, min_length=1, max_length=256)
    edit_method: Literal["auto", "qwen", "warp", "warp_qwen"] = "auto"
    comparison_seed: StrictInt | None = Field(default=None, ge=0, le=MAX_SEED)


@router.get("/state")
async def get_state(chat_id: str, user=Depends(get_verified_user)):
    return {"chat_id": chat_id, **await read_state(chat_id, user.id)}


@router.post("/acceptance")
async def post_acceptance(form: AcceptanceForm, user=Depends(get_verified_user)):
    return await approve_image(form.chat_id, user.id, form.number, form.verdict)


@router.get("/preferences")
async def get_preferences(chat_id: str, user=Depends(get_verified_user)):
    return await read_preferences(chat_id, user.id)


@router.post("/preferences")
async def post_preferences(form: PreferencesForm, user=Depends(get_verified_user)):
    changes = {key: getattr(form, key) for key in form.model_fields_set if key != "chat_id"}
    return await write_preferences(form.chat_id, user.id, changes)


@router.get("/calibration")
async def get_calibration(user=Depends(get_verified_user)):
    calibration = _calibration()
    return await asyncio.to_thread(calibration.calibration_state, str(DATA_DIR), user.id)


@router.post("/calibration/reset")
async def reset_calibration(user=Depends(get_verified_user)):
    calibration = _calibration()
    await asyncio.to_thread(calibration.reset_calibration, str(DATA_DIR), user.id)
    return {"reset": True}
