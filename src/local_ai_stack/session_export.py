"""Evidence exports, not backups: never copy the Open WebUI authentication database.

Ownership is lexical and deliberately narrow: managed uploads, ComfyUI's three
media directories, the native ledgers, named generated configuration, the image
chat filter's turn trace and logs.
Database tables are projected in a single read transaction (including live WAL).
Media bytes are preserved; no image is decoded. A running service may create new
media after its database snapshot, so filesystem-wide simultaneity is not claimed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import tempfile
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import yaml

from .layout import StackLayout
from .runner import sanitize


class SessionExportError(RuntimeError):
    """No completed archive was published because evidence could not be read safely."""


# Rejection budgets shared by export and download verification. Never truncate.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000  # Includes manifest.json.
MAX_TOTAL_UNCOMPRESSED_BYTES = 20 * 1024**3
MAX_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024


_REDACTED = "[redacted]"
_SECRET_KEY = re.compile(
    r"(?i)(?:password|passwd|pwd|api.?keys?|secret|credential|authorization|"
    r"authentication|access.?token|refresh.?token|id.?token|session|cookie|"
    r"private.?key|client.?secret|bearer|jwt|^auth$|^tokens?$|share_id|api.?configs?|^headers$)"
)
_ASSIGNMENT = re.compile(
    r"""(?ix)(["']?(?:[\w.-]*(?:password|passwd|api[_-]?keys?|secret|credential|"""
    r"""access[_-]?token|refresh[_-]?token|session|cookie|authorization|private[_-]?key)"""
    r"""[\w.-]*|token|auth|pwd)["']?\s*[:=]\s*)"""
    r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)"""
)
_SIGNED_QUERY = re.compile(
    r"(?i)([?&](?:x-amz-[\w-]+|x-goog-[\w-]+|awsaccesskeyid|signature|"
    r"sig|policy|key-pair-id|credential|access_token|token|api_key|se|sp|sv|sr)=)[^&\s\"'<>]+"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_HASH = re.compile(r"\$(?:2[aby]\$\d\d\$[^\s\"']+|argon2(?:id|i|d)\$[^\s\"']+)")
_PEM = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)", re.DOTALL
)
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")
_GROUP = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_EXCLUDED_DIRS = {"cache", ".cache", "__pycache__", "models", "node_modules", ".git", ".venv"}
_EXCLUDED_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".dpapi",
    ".gguf",
    ".safetensors",
    ".ckpt",
    ".pt",
    ".pth",
    ".bin",
    ".onnx",
    ".h5",
    ".tflite",
    ".whl",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db3",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".sqlite3-journal",
}

# Explicit column lists prevent newly added auth/config columns from leaking.
_WEBUI = {
    "chat": (
        "id",
        "user_id",
        "title",
        "chat",
        "created_at",
        "updated_at",
        "archived",
        "pinned",
        "meta",
        "folder_id",
        "summary",
        "current_message_id",
    ),
    "chat_message": (
        "id",
        "chat_id",
        "user_id",
        "role",
        "parent_id",
        "content",
        "output",
        "model_id",
        "files",
        "sources",
        "embeds",
        "meta",
        "done",
        "status_history",
        "error",
        "usage",
        "created_at",
        "updated_at",
    ),
    "message": (
        "id",
        "user_id",
        "channel_id",
        "reply_to_id",
        "parent_id",
        "content",
        "data",
        "meta",
        "created_at",
        "updated_at",
    ),
    "file": (
        "id",
        "user_id",
        "hash",
        "filename",
        "path",
        "data",
        "meta",
        "created_at",
        "updated_at",
    ),
    "chatidfile": ("id", "chat_id", "file_id", "user_id", "created_at", "updated_at"),
    "model": (
        "id",
        "user_id",
        "base_model_id",
        "name",
        "meta",
        "created_at",
        "updated_at",
        "is_active",
    ),
    "config": ("key", "value", "updated_at"),
}
_NATIVE = {
    "gallery": ("id", "message_id", "entries"),
    "image": ("number", "file_id", "message_id", "entry"),
    "state": ("key", "value"),
}
_CALIBRATION = {
    "metadata": ("key", "value"),
    "legacy": ("account_prefix", "evidence"),
    "account": ("user_id", "samples"),
    "attempt": ("user_id", "attempt_id", "chat_id", "facts", "created"),
    "feedback": ("user_id", "attempt_id", "verdict", "created", "learned"),
    "calibration": (
        "user_id",
        "family",
        "workflow",
        "change",
        "operation",
        "bias",
        "accepted",
        "updated",
        "sample",
    ),
}
_JSON_COLUMNS = {
    "chat",
    "meta",
    "data",
    "output",
    "files",
    "sources",
    "embeds",
    "status_history",
    "error",
    "usage",
    "entries",
    "entry",
    "evidence",
    "facts",
}
_SAFE_CONFIG = {
    "image_generation.enable",
    "image_generation.prompt.enable",
    "image_generation.engine",
    "image_generation.model",
    "image_generation.size",
    "image_generation.steps",
    "image_generation.comfyui.base_url",
    "image_generation.comfyui.workflow",
    "image_generation.comfyui.nodes",
    "images.edit.enable",
    "images.edit.engine",
    "images.edit.model",
    "images.edit.size",
    "images.edit.comfyui.base_url",
    "images.edit.comfyui.workflow",
    "images.edit.comfyui.nodes",
    "ollama.enable",
    "ollama.base_urls",
    "models.default",
    "models.order",
}


def _safe_text(value: str) -> str:
    value = _PEM.sub(_REDACTED, value)
    value = re.sub(
        r"(?i)\b(authorization\s*[:=]\s*)(?:Basic|Bearer)\s+[A-Za-z0-9._~+/-]+=*",
        r"\1[redacted]",
        value,
    )
    value = re.sub(r"(?im)\b((?:set-cookie|cookie)\s*:\s*)[^\r\n]+", r"\1[redacted]", value)
    value = re.sub(
        r"(?i)(--(?:api[-_]?key|token|password|secret|credential)[\w-]*\s+)(?:\"[^\"]*\"|'[^']*'|\S+)",
        r"\1[redacted]",
        value,
    )
    value = sanitize(value)
    value = _SIGNED_QUERY.sub(r"\1[redacted]", value)
    value = _ASSIGNMENT.sub(r"\1[redacted]", value)
    value = _JWT.sub(_REDACTED, value)
    value = _HASH.sub(_REDACTED, value)
    value = _AWS_KEY.sub(_REDACTED, value)
    return value


def _clean(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _SECRET_KEY.search(str(key)) else _clean(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        # Configuration sometimes embeds another JSON document as a string.
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(_clean(json.loads(value)), ensure_ascii=False)
            except ValueError:
                pass
        return _safe_text(value)
    return value


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def _manifest_bytes(value: dict) -> bytes:
    result = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False)
    for piece in encoder.iterencode(value):
        encoded = piece.encode("utf-8")
        if len(result) + len(encoded) > MAX_MANIFEST_BYTES:
            raise SessionExportError(
                "Session manifest exceeds the safe size budget; export was not published."
            )
        result.extend(encoded)
    return bytes(result)


def _zip_preflight(source: BinaryIO) -> int:
    """Bound central-directory allocation before ZipFile parses an untrusted ZIP."""
    source.seek(0, os.SEEK_END)
    length = source.tell()
    source.seek(max(0, length - (65535 + 22)))
    ending = source.read(65535 + 22)
    position = ending.rfind(b"PK\x05\x06")
    while position >= 0:
        if len(ending) - position >= 22:
            fields = struct.unpack("<4s4H2LH", ending[position : position + 22])
            if position + 22 + fields[-1] == len(ending):
                break
        position = ending.rfind(b"PK\x05\x06", 0, position)
    if position < 0:
        raise SessionExportError("Invalid session archive: missing ZIP end record.")
    _, disk, central_disk, disk_count, count, central_size, central_offset, _ = fields
    end_position = length - len(ending) + position
    boundary = end_position
    if disk or central_disk or disk_count != count:
        raise SessionExportError("Invalid session archive: multipart ZIP is not supported.")
    if count == 65535 or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        source.seek(max(0, end_position - 20))
        locator = source.read(20)
        if len(locator) == 20 and locator[:4] == b"PK\x06\x07":
            _, locator_disk, zip64_offset, disks = struct.unpack("<4sLQL", locator)
            if locator_disk or disks != 1 or zip64_offset + 56 > end_position - 20:
                raise SessionExportError("Invalid session archive: invalid ZIP64 locator.")
            source.seek(zip64_offset)
            record = source.read(56)
            if len(record) != 56:
                raise SessionExportError("Invalid session archive: incomplete ZIP64 record.")
            (
                signature,
                record_size,
                _,
                _,
                disk,
                central_disk,
                disk_count,
                count,
                central_size,
                central_offset,
            ) = struct.unpack("<4sQ2H2L4Q", record)
            if (
                signature != b"PK\x06\x06"
                or record_size < 44
                or zip64_offset + 12 + record_size != end_position - 20
                or disk
                or central_disk
                or disk_count != count
            ):
                raise SessionExportError("Invalid session archive: invalid ZIP64 end record.")
            boundary = zip64_offset
        elif central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
            raise SessionExportError("Invalid session archive: missing ZIP64 locator.")
    if count > MAX_ARCHIVE_MEMBERS or central_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise SessionExportError(
            "Session archive exceeds the member-count or central-directory budget."
        )
    if central_offset + central_size > boundary:
        raise SessionExportError("Invalid session archive: central directory is out of bounds.")
    source.seek(central_offset)
    central_end = central_offset + central_size
    # Do not trust the advertised count: otherwise ZipFile could allocate many
    # more entries than its forged end record claims before we inspect infolist.
    actual_count = 0
    while source.tell() < central_end:
        header = source.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise SessionExportError("Invalid session archive: malformed central directory.")
        name_size, extra_size, comment_size = struct.unpack("<3H", header[28:34])
        actual_count += 1
        if actual_count > MAX_ARCHIVE_MEMBERS:
            raise SessionExportError("Session archive exceeds the member-count budget.")
        next_position = source.tell() + name_size + extra_size + comment_size
        if next_position > central_end:
            raise SessionExportError("Invalid session archive: incomplete central-directory entry.")
        source.seek(next_position)
    if actual_count != count:
        raise SessionExportError("Invalid session archive: ZIP member count mismatch.")
    source.seek(0)
    return count


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise SessionExportError("Path traversal is not allowed.")
    return Path(os.path.abspath(path))


def _check_path(path: Path) -> os.stat_result:
    """Reject links/reparse points in every ancestor, not just the last component."""
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SessionExportError("Symlinks and Windows reparse points are not exportable.")
    return info


@contextmanager
def _open_file(path: Path) -> Iterator[BinaryIO]:
    before = _check_path(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SessionExportError("Only regular, non-hardlinked owned files are exportable.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        after = _check_path(path)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or (
            after.st_dev,
            after.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise SessionExportError("An owned file changed identity during export.")
        yield stream


def _archive_name(path: Path) -> str:
    if path.is_absolute() or any(
        part in {"", ".", ".."} or "\\" in part or ":" in part for part in path.parts
    ):
        raise SessionExportError("Unsafe archive member name.")
    return path.as_posix()


def _excluded_media_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith(".env")
        or name
        in {
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "authorized_keys",
            ".webui_secret_key",
            ".netrc",
            ".npmrc",
            "credentials.json",
            "credentials",
            "secrets.json",
            "tokens.json",
        }
        or path.suffix.lower() in _EXCLUDED_SUFFIXES
    )


class _Collector:
    def __init__(self, root: Path, archive: zipfile.ZipFile, excluded: set[Path]):
        self.root = root
        self.archive = archive
        self.excluded = excluded
        self.members: list[dict] = []
        self.omissions: list[dict] = []
        self.errors: list[dict] = []
        self.names: set[str] = set()
        self.total_bytes = 0

    def relative(self, path: Path) -> str:
        return _archive_name(path.relative_to(self.root))

    def omit(self, source: str, reason: str, **details: object) -> None:
        self.omissions.append({"source": _safe_text(source), "reason": reason, **details})

    def add(self, name: str, chunks: Iterator[bytes], kind: str) -> None:
        name = _archive_name(Path(name))
        if name in self.names:
            raise SessionExportError("Duplicate archive member.")
        if len(self.names) + 2 > MAX_ARCHIVE_MEMBERS:
            raise SessionExportError("Session evidence exceeds the archive member-count budget.")
        self.names.add(name)
        digest = hashlib.sha256()
        size = 0
        with self.archive.open(name, "w", force_zip64=True) as output:
            for chunk in chunks:
                if self.total_bytes + len(chunk) > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise SessionExportError(
                        "Session evidence exceeds the uncompressed-byte budget."
                    )
                self.total_bytes += len(chunk)
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        self.members.append(
            {"path": name, "sha256": digest.hexdigest(), "size": size, "kind": kind}
        )

    def json(self, name: str, value: object, kind: str) -> None:
        self.add(name, iter((_json(value),)), kind)

    def walk(self, path: Path) -> Iterator[Path]:
        try:
            info = _check_path(path)
        except FileNotFoundError:
            self.omit(self.relative(path), "not-present")
            return
        if not stat.S_ISDIR(info.st_mode):
            raise SessionExportError("An owned data directory is not a directory.")
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            if child in self.excluded:
                self.omit(self.relative(child), "export-output")
                continue
            info = _check_path(child)
            if stat.S_ISDIR(info.st_mode):
                if child.name.lower() in _EXCLUDED_DIRS:
                    self.omit(self.relative(child), "excluded-cache-or-model-directory")
                else:
                    yield from self.walk(child)
            elif stat.S_ISREG(info.st_mode):
                yield child
            else:
                raise SessionExportError("Special files are not exportable.")

    def media(self, directory: Path) -> None:
        for path in self.walk(directory):
            if _excluded_media_file(path):
                self.omit(self.relative(path), "excluded-secret-database-or-model-file")
                continue
            with _open_file(path) as stream:
                before = os.fstat(stream.fileno())
                if before.st_size > MAX_TOTAL_UNCOMPRESSED_BYTES - self.total_bytes:
                    raise SessionExportError("Media evidence exceeds the uncompressed-byte budget.")
                self.add(self.relative(path), iter(lambda: stream.read(1024 * 1024), b""), "media")
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise SessionExportError(
                        "Media changed during export; retry when the writer is idle."
                    )

    def text(self, path: Path, *, structured: bool = False) -> None:
        source = self.relative(path)
        try:
            with _open_file(path) as stream:
                remaining = MAX_TOTAL_UNCOMPRESSED_BYTES - self.total_bytes
                if os.fstat(stream.fileno()).st_size > remaining:
                    raise SessionExportError("Text evidence exceeds the uncompressed-byte budget.")
                data = bytearray()
                while chunk := stream.read(min(1024 * 1024, remaining - len(data) + 1)):
                    if len(data) + len(chunk) > remaining:
                        raise SessionExportError(
                            "Text evidence exceeds the uncompressed-byte budget."
                        )
                    data.extend(chunk)
                raw = data.decode("utf-8")
            if structured:
                value = yaml.safe_load(raw) if path.suffix == ".yaml" else json.loads(raw)
                self.json(
                    source + ".json" if path.suffix == ".yaml" else source,
                    _clean(value),
                    "configuration",
                )
            else:
                self.add(source, iter((_safe_text(raw).encode("utf-8"),)), "diagnostic")
        except FileNotFoundError:
            self.omit(source, "not-present")
        except SessionExportError:
            raise
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            # Never copy exception text: parse errors can contain secret source lines.
            self.errors.append(
                {
                    "source": source,
                    "reason": "unreadable-optional-text",
                    "error_type": type(exc).__name__,
                }
            )

    def trace(self, path: Path) -> None:
        """One file of the image chat filter's turn trace, redacted record by record.

        The filter appends while the export runs, so only the bytes present when the
        file is opened are read; a record cut mid-write, or any line that is not
        JSON, is redacted as text.
        """
        source = self.relative(path)
        try:
            with _open_file(path) as stream:
                size = os.fstat(stream.fileno()).st_size
                if size > MAX_TOTAL_UNCOMPRESSED_BYTES - self.total_bytes:
                    raise SessionExportError("Trace evidence exceeds the uncompressed-byte budget.")
                data = bytearray()
                while len(data) < size and (
                    chunk := stream.read(min(1024 * 1024, size - len(data)))
                ):
                    data.extend(chunk)
        except FileNotFoundError:
            self.omit(source, "not-present")
            return
        except OSError as exc:
            self.errors.append(
                {
                    "source": source,
                    "reason": "unreadable-optional-text",
                    "error_type": type(exc).__name__,
                }
            )
            return
        records = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                records.append(_safe_text(line))
            else:
                records.append(json.dumps(_clean(record), ensure_ascii=False, sort_keys=True))
        payload = "".join(f"{record}\n" for record in records)
        self.add(source, iter((payload.encode("utf-8"),)), "trace")

    def file_reference(self, row: dict) -> None:
        raw = row.get("path")
        row["exported_asset"] = None
        filename = row.get("filename")
        if isinstance(filename, str) and _excluded_media_file(Path(filename)):
            row.update(filename="[excluded]", path="[excluded storage]", data=None, meta=None)
            self.omit(
                "open-webui/file", "file-metadata-excluded-by-policy", file_id=_clean(row.get("id"))
            )
            return
        if not raw:
            self.omit("open-webui/file", "file-has-no-local-path", file_id=_clean(row.get("id")))
            return
        if not isinstance(raw, str) or "://" in raw:
            row["path"] = "[external storage]"
            self.omit(
                "open-webui/file",
                "external-file-reference-not-fetched",
                file_id=_clean(row.get("id")),
            )
            return
        uploads = self.root / "data" / "open-webui" / "uploads"
        path = Path(raw)
        if ".." in path.parts:
            raise SessionExportError("Traversal in Open WebUI file metadata.")
        if not path.is_absolute():
            # No basename guessing and no consulting the process working directory.
            if path.parts and path.parts[0] == "uploads":
                path = uploads.parent / path
            elif path.parts[:3] == ("data", "open-webui", "uploads"):
                path = self.root / path
            else:
                row["path"] = "[unowned storage]"
                self.omit(
                    "open-webui/file", "unowned-file-reference", file_id=_clean(row.get("id"))
                )
                return
        if not path.is_relative_to(uploads):
            row["path"] = "[unowned storage]"
            self.omit("open-webui/file", "unowned-file-reference", file_id=_clean(row.get("id")))
            return
        info = _check_path(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SessionExportError("An Open WebUI file reference is not an owned regular file.")
        if (
            path in self.excluded
            or _excluded_media_file(path)
            or any(part.lower() in _EXCLUDED_DIRS for part in path.relative_to(uploads).parts[:-1])
        ):
            row["path"] = "[excluded storage]"
            row.update(data=None, meta=None)
            self.omit(
                "open-webui/file",
                "file-reference-excluded-by-policy",
                file_id=_clean(row.get("id")),
            )
            return
        row["path"] = self.relative(path)
        row["exported_asset"] = self.relative(path)

    def database(self, path: Path, schema: dict[str, tuple[str, ...]], required: set[str]) -> None:
        source = self.relative(path)
        # Hold a verified descriptor while SQLite opens the same verified inode.
        with _open_file(path) as held:
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    info = _check_path(Path(str(path) + suffix))
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise SessionExportError("Unsafe SQLite sidecar.")
                except FileNotFoundError:
                    pass
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
            try:
                opened = os.fstat(held.fileno())
                current = _check_path(path)
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise SessionExportError("Database changed identity during export.")
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not required.issubset(tables):
                    raise SessionExportError(
                        "A core evidence database has an unsupported or incomplete schema."
                    )
                for table in sorted(tables - schema.keys()):
                    self.omit(source, "excluded-database-table", table=_safe_text(table))
                masks: list[tuple[str, bytes]] = []

                def chunks() -> Iterator[bytes]:
                    yield b'{"tables":{'
                    first_table = True
                    for table, allowed in schema.items():
                        if table not in tables:
                            self.omit(source, "table-not-present", table=table)
                            continue
                        columns = {
                            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
                        }
                        if (
                            schema is _WEBUI
                            and table == "config"
                            and not {"key", "value"}.issubset(columns)
                        ):
                            self.omit(source, "legacy-config-blob-excluded", table=table)
                            continue
                        selected = [column for column in allowed if column in columns]
                        if (
                            not selected
                            or (table == "chat" and "chat" not in columns)
                            or (table == "file" and not {"id", "path"}.issubset(columns))
                        ):
                            raise SessionExportError(
                                "A core evidence table has an unsupported schema."
                            )
                        if columns - set(selected):
                            self.omit(
                                source,
                                "excluded-database-columns",
                                table=table,
                                columns=sorted(columns - set(selected)),
                            )
                        if not first_table:
                            yield b","
                        first_table = False
                        yield _json(table) + b":["
                        query = ",".join(f'"{column}"' for column in selected)
                        # A stable full-column order works even on older schemas without id.
                        cursor = connection.execute(
                            f'SELECT {query} FROM "{table}" ORDER BY {query}'
                        )
                        first_row = True
                        for record in cursor:
                            row = dict(record)
                            if schema is _WEBUI and table == "config":
                                if row["key"] not in _SAFE_CONFIG:
                                    self.omit(source, "excluded-config-record")
                                    continue
                                # SQLite stores JSON scalars with numeric affinity as numbers.
                                if isinstance(row["value"], (str, bytes, bytearray)):
                                    row["value"] = json.loads(row["value"])
                            if schema is _NATIVE and table == "state":
                                if row.get("key") not in {
                                    "current",
                                    "selection",
                                    "accepted",
                                    "preferences",
                                    "draft",
                                    "temporary-owner",
                                }:
                                    self.omit(source, "excluded-native-state-key")
                                    continue
                                row["value"] = json.loads(row["value"])
                                draft = row["value"]
                                if (
                                    row["key"] == "draft"
                                    and isinstance(draft, dict)
                                    and isinstance(draft.get("mask_png"), str)
                                ):
                                    header, separator, encoded = draft["mask_png"].partition(",")
                                    if header != "data:image/png;base64" or not separator:
                                        raise SessionExportError(
                                            "Native draft mask encoding is unreadable."
                                        )
                                    mask_name = source + ".masks/draft.png"
                                    masks.append(
                                        (mask_name, base64.b64decode(encoded, validate=True))
                                    )
                                    draft["exported_mask"] = mask_name
                            for key, value in row.items():
                                if value is not None and (
                                    key in _JSON_COLUMNS
                                    or (table == "chat_message" and key == "content")
                                ):
                                    row[key] = (
                                        json.loads(value) if isinstance(value, str) else value
                                    )
                            if schema is _WEBUI and table == "file":
                                self.file_reference(row)
                            if not first_row:
                                yield b","
                            first_row = False
                            yield _json(_clean(row))
                        yield b"]"
                    yield b"}}"

                self.add(source + ".json", chunks(), "database-projection")
                for mask_name, data in masks:
                    self.add(mask_name, iter((data,)), "mask")
            finally:
                connection.close()

    def native(self) -> None:
        directory = self.root / "data" / "open-webui" / "pepito-native"
        for path in self.walk(directory):
            relative = path.relative_to(directory)
            if (
                len(relative.parts) == 2
                and _HEX64.fullmatch(relative.parts[0])
                and path.suffix == ".sqlite3"
                and _HEX64.fullmatch(path.stem)
            ):
                self.database(path, _NATIVE, set(_NATIVE))
            elif (
                len(relative.parts) == 3
                and _HEX64.fullmatch(relative.parts[0])
                and relative.parts[1] == "groups"
                and _GROUP.fullmatch(path.name)
            ):
                self.text(path)
            else:
                self.omit(self.relative(path), "native-sidecar-or-unknown-file")

    def filter_state(self) -> None:
        directory = self.root / "data" / "open-webui" / "pepito-characters"
        for path in self.walk(directory):
            if len(path.relative_to(directory).parts) != 1 or not re.fullmatch(
                r"[a-f0-9]{16}(?:\.pictures)?\.json", path.name
            ):
                self.omit(self.relative(path), "filter-sidecar-or-unknown-file")
                continue
            with _open_file(path) as stream:
                before = os.fstat(stream.fileno())
                remaining = MAX_TOTAL_UNCOMPRESSED_BYTES - self.total_bytes
                if before.st_size > remaining:
                    raise SessionExportError(
                        "Filter evidence exceeds the uncompressed-byte budget."
                    )
                raw = bytearray()
                while chunk := stream.read(min(1024 * 1024, remaining - len(raw) + 1)):
                    if len(raw) + len(chunk) > remaining:
                        raise SessionExportError(
                            "Filter evidence exceeds the uncompressed-byte budget."
                        )
                    raw.extend(chunk)
                after = os.fstat(stream.fileno())
                if len(raw) > remaining or (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise SessionExportError(
                        "Filter evidence changed during export or exceeded its byte budget."
                    )
            saved = json.loads(raw.decode("utf-8"))
            if not isinstance(saved, dict):
                raise SessionExportError("A filter evidence record is not a JSON object.")
            last = saved.get("last_edit")
            if isinstance(last, dict) and last.get("mask_png") is not None:
                if not isinstance(last["mask_png"], str):
                    raise SessionExportError("A persisted edit mask has unreadable encoding.")
                bits = base64.b64decode(last["mask_png"], validate=True)
                mask_name = self.relative(path) + ".masks/last-edit.png"
                last["exported_mask"] = mask_name
                self.add(mask_name, iter((bits,)), "mask")
            self.json(self.relative(path), _clean(saved), "filter-state")


def export_session(layout: StackLayout, destination: Path) -> Path:
    """Atomically publish a ZIP of session evidence, returning its absolute path.

    ``destination`` is a file, not a directory. Existing destination bytes survive
    any failure. Missing optional roots and redaction/exclusion policies are in
    manifest.json; unreadable core DB/media or unsafe paths abort the export.
    This intentionally excludes auth/user/session tables, secret configuration
    records, environment files, models and caches. It is not a restore format.
    """
    root = _absolute(layout.root)
    target = _absolute(Path(destination))
    temporary: Path | None = None
    try:
        _check_path(root)
        _check_path(target.parent)
        try:
            info = _check_path(target)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SessionExportError("Export destination must be a regular file.")
        except FileNotFoundError:
            pass
        # Never overwrite managed evidence, configuration or databases. A ZIP in
        # the install root, logs or a media tree is safe and explicitly excluded.
        if target.suffix.lower() != ".zip":
            raise SessionExportError("Export destination must have a .zip extension.")
        descriptor, name = tempfile.mkstemp(
            prefix=".session-export-", suffix=".partial", dir=target.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                collector = _Collector(root, archive, {target, temporary})
                collector.database(
                    root / "data" / "open-webui" / "webui.db", _WEBUI, {"chat", "file"}
                )
                collector.native()
                collector.filter_state()
                for name in ("turns.jsonl", "turns.jsonl.1"):
                    collector.trace(root / "data" / "open-webui" / "pepito-trace" / name)
                calibration = (
                    root / "data" / "open-webui" / "pepito-calibration" / "calibration.sqlite3"
                )
                try:
                    _check_path(calibration)
                except FileNotFoundError:
                    collector.omit(collector.relative(calibration), "not-present")
                else:
                    collector.database(calibration, _CALIBRATION, set(_CALIBRATION))
                collector.text(
                    root / "data" / "open-webui" / "pepito-fidelity.json", structured=True
                )
                for directory in (
                    root / "data" / "open-webui" / "uploads",
                    *(
                        root / base / "comfyui" / child
                        for base in ("data", "apps")
                        for child in ("input", "output", "temp")
                    ),
                ):
                    collector.media(directory)
                for name in (
                    "open-webui-workflow.json",
                    "open-webui-workflow-nodes.json",
                    "pepito-image-stacks.json",
                ):
                    collector.text(root / "data" / "open-webui" / name, structured=True)
                collector.text(
                    root / "data" / "comfyui" / "extra_model_paths.yaml", structured=True
                )
                collector.text(root / "state" / "config.yaml", structured=True)
                for app in ("open-webui", "comfyui", "ollama"):
                    collector.text(root / "apps" / app / ".pepito-managed.json", structured=True)
                for path in collector.walk(root / "logs"):
                    if re.fullmatch(r"[\w.-]+\.log(?:\.\d+)?", path.name):
                        collector.text(path)
                    else:
                        collector.omit(collector.relative(path), "not-a-service-log")
                # No raw DB, environment or broad state/runtime/config dump.
                collector.omit("policy", "auth-user-session-and-secret-config-records-excluded")
                collector.omit(
                    "policy", "environment-private-keys-model-weights-and-caches-excluded"
                )
                collector.omit("policy", "non-allowlisted-roots-and-files-excluded")
                manifest = {
                    "schema_version": 1,
                    "created_at": datetime.now(UTC).isoformat(),
                    "status": "complete-with-omissions"
                    if collector.omissions or collector.errors
                    else "complete",
                    "consistency": "Each SQLite database uses one live-WAL read transaction; media and separate databases are not a global snapshot.",
                    "limits": {
                        "truncation": False,
                        "row_limit": None,
                        "byte_limit": MAX_TOTAL_UNCOMPRESSED_BYTES,
                        "manifest_byte_limit": MAX_MANIFEST_BYTES,
                        "member_count_limit": MAX_ARCHIVE_MEMBERS,
                        "central_directory_byte_limit": MAX_CENTRAL_DIRECTORY_BYTES,
                    },
                    "redactions": [
                        "Credential fields and known credential literals in structured evidence/configuration and diagnostics",
                        "Signed URL credential query values",
                        "Password hashes and private-key blocks",
                    ],
                    "members": collector.members,
                    "omissions": collector.omissions,
                    "errors": collector.errors,
                    "manifest_integrity": "members cover every payload; manifest.json excludes itself to avoid a recursive digest",
                    "evidence_notes": [
                        "Native pending draft masks and each chat's latest persisted filter last_edit.mask_png are collected when present.",
                        "Consumed drafts and older masks overwritten by later filter recipes cannot be reconstructed if no persisted copy remains; absence is not evidence that no mask was used.",
                        "The image chat filter's turn trace (data/open-webui/pepito-trace/turns.jsonl and its one rotation, turns.jsonl.1) is collected record by record with the same redaction; a record being written when the export read the file may be cut.",
                    ],
                }
                manifest_data = _manifest_bytes(manifest)
                if collector.total_bytes + len(manifest_data) > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise SessionExportError(
                        "Session evidence and manifest exceed the uncompressed-byte budget."
                    )
                archive.writestr("manifest.json", manifest_data)
            stream.flush()
            _zip_preflight(stream)
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        return target
    except SessionExportError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError) as exc:
        # Do not expose SQLite values, absolute user paths or parser excerpts.
        raise SessionExportError(
            f"Session export failed safely ({type(exc).__name__}); no archive was published."
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_session_archive(path: Path) -> dict:
    """Validate a schema-1 session ZIP in place, without extracting any member.

    Checks exact member membership, portable names, regular-file entries, CRC,
    uncompressed sizes and SHA-256. This proves integrity, not sender identity;
    authenticate the transport or compare a separately obtained archive digest.
    """

    def invalid(reason: str) -> None:
        raise SessionExportError("Invalid session archive: " + reason + ".")

    def member_name(name: object) -> str:
        if (
            not isinstance(name, str)
            or not name
            or any(character in name for character in ("\\", ":", "\x00"))
            or any(part in {"", ".", ".."} for part in name.split("/"))
        ):
            invalid("unsafe member name")
        return name

    def object_pairs(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                invalid("duplicate manifest JSON key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        invalid("nonfinite manifest JSON value")

    try:
        with _open_file(_absolute(Path(path))) as source:
            expected_count = _zip_preflight(source)
            with zipfile.ZipFile(source, "r") as archive:
                entries: dict[str, zipfile.ZipInfo] = {}
                if len(archive.infolist()) != expected_count:
                    invalid("ZIP member count mismatch")
                advertised_total = 0
                for info in archive.infolist():
                    name = member_name(info.filename)
                    if name != info.orig_filename or name in entries:
                        invalid("duplicate or ambiguous ZIP entry")
                    mode = stat.S_IFMT(info.external_attr >> 16)
                    if info.is_dir() or mode not in {0, stat.S_IFREG} or info.flag_bits & 1:
                        invalid("nonregular or encrypted ZIP entry")
                    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        invalid("unsupported ZIP compression")
                    entries[name] = info
                    advertised_total += info.file_size
                    if advertised_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                        invalid("uncompressed-byte budget exceeded")
                if "manifest.json" not in entries:
                    invalid("missing manifest.json")
                manifest_info = entries["manifest.json"]
                if manifest_info.file_size > MAX_MANIFEST_BYTES:
                    invalid("manifest-byte budget exceeded")
                manifest_data = bytearray()
                with archive.open(manifest_info, "r") as member:
                    while chunk := member.read(
                        min(1024 * 1024, MAX_MANIFEST_BYTES - len(manifest_data) + 1)
                    ):
                        if len(manifest_data) + len(chunk) > MAX_MANIFEST_BYTES:
                            invalid("manifest-byte budget exceeded")
                        manifest_data.extend(chunk)
                manifest = json.loads(
                    manifest_data.decode("utf-8"),
                    object_pairs_hook=object_pairs,
                    parse_constant=constant,
                )
                if (
                    not isinstance(manifest, dict)
                    or type(manifest.get("schema_version")) is not int
                    or manifest["schema_version"] != 1
                ):
                    invalid("unsupported manifest schema")
                if manifest.get("status") not in {"complete", "complete-with-omissions"}:
                    invalid("manifest does not describe a completed export")
                if (
                    not isinstance(manifest.get("omissions"), list)
                    or not isinstance(manifest.get("errors"), list)
                    or not isinstance(manifest.get("limits"), dict)
                ):
                    invalid("missing manifest omission, error or limit declarations")
                members = manifest.get("members")
                if not isinstance(members, list) or not members:
                    invalid("missing payload member list")
                declared: dict[str, dict] = {}
                for item in members:
                    if not isinstance(item, dict) or set(item) != {
                        "path",
                        "sha256",
                        "size",
                        "kind",
                    }:
                        invalid("invalid payload member declaration")
                    name = member_name(item["path"])
                    if name == "manifest.json" or name in declared:
                        invalid("duplicate or recursive manifest member")
                    if (
                        not isinstance(item["sha256"], str)
                        or not _HEX64.fullmatch(item["sha256"])
                        or type(item["size"]) is not int
                        or item["size"] < 0
                        or not isinstance(item["kind"], str)
                        or not item["kind"]
                    ):
                        invalid("invalid payload hash, size or kind")
                    declared[name] = item
                if set(entries) != set(declared) | {"manifest.json"}:
                    invalid("missing or unexpected payload entries")
                streamed_total = len(manifest_data)
                for name, item in declared.items():
                    info = entries[name]
                    if info.file_size != item["size"]:
                        invalid("payload size mismatch")
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(info, "r") as member:
                        while chunk := member.read(
                            min(1024 * 1024, MAX_TOTAL_UNCOMPRESSED_BYTES - streamed_total + 1)
                        ):
                            size += len(chunk)
                            streamed_total += len(chunk)
                            if streamed_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                                invalid("streamed uncompressed-byte budget exceeded")
                            if size > item["size"]:
                                invalid("payload exceeds declared size")
                            digest.update(chunk)
                    if size != item["size"] or digest.hexdigest() != item["sha256"]:
                        invalid("payload SHA-256 or size mismatch")
                return manifest
    except SessionExportError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        RecursionError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
    ):
        # ZIP/JSON errors may contain hostile filenames or manifest excerpts.
        raise SessionExportError(
            "Invalid or unreadable session archive; integrity verification failed."
        ) from None
