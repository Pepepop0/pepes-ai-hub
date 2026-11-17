"""A temporary public link to the Linux bench's Open WebUI, for a guest to sign up and play.

``bash scripts/capella-linux.sh share ROOT`` on the pod, with the services started and
the administrator already created:

1. Open WebUI accepts sign-ups as ordinary users (``ENABLE_SIGNUP``, ``DEFAULT_USER_ROLE``
   ``user``), written through the authenticated admin API the same way Admin -> Settings ->
   General saves them, so a guest is not left on the "pending" screen;
2. a pinned ``cloudflared`` quick tunnel (no Cloudflare account) forwards an
   ``https://<words>.trycloudflare.com`` address to Open WebUI only. ComfyUI and Ollama stay
   on loopback. The link is printed once cloudflared reports it; Ctrl+C ends it.

Anyone with the link can create an account while it runs: meant for a pod that is deleted
afterwards, not for a lasting installation.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import httpx

from .endpoints import StackEndpoints
from .layout import StackLayout
from .linux_bench import DEFAULT_PROFILE, BenchProfileError, bench_profile
from .open_webui_filters import FilterSync, FilterSyncReport, local_admin_token

# cloudflared 2026.10.7 (30/10/2026); the digest GitHub publishes for the asset and the
# one in the release notes agree.
CLOUDFLARED_VERSION = "2026.10.7"
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/download/"
    f"{CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
)
CLOUDFLARED_SHA256 = "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872"
CONFIG_ENDPOINT = "/api/v1/auths/admin/config"
OPEN_FIELDS: Mapping[str, object] = {"ENABLE_SIGNUP": True, "DEFAULT_USER_ROLE": "user"}
# Open WebUI 0.12.0 routers/auths.py AdminConfig: the MFA switches are only written when
# sent, so they are left out; I18N refuses null; these strings are required.
_UNSENT = ("ENABLE_MFA", "MFA_ALLOW_OAUTH_BYPASS", "MFA_ALLOW_TRUSTED_HEADER_BYPASS")
_REQUIRED_TEXT = ("WEBUI_URL", "API_KEYS_ALLOWED_ENDPOINTS", "DEFAULT_GROUP_ID")
_LINK = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _admin_config(response: httpx.Response) -> Mapping[str, object]:
    response.raise_for_status()
    config = response.json()
    if not isinstance(config, Mapping):
        raise ValueError(  # noqa: TRY004 - a malformed server answer, reported as UNAVAILABLE
            "Open WebUI returned an admin configuration that is not an object"
        )
    return config


def signups_open(config: Mapping[str, object]) -> bool:
    return all(config.get(field) == value for field, value in OPEN_FIELDS.items())


def open_signups(client: httpx.Client) -> FilterSync:
    """Let new accounts in as users, every other admin setting sent back as it was."""
    config = _admin_config(client.get(CONFIG_ENDPOINT))
    if signups_open(config):
        return FilterSync.UNCHANGED
    form: dict[str, object] = {}
    for field, value in config.items():
        if field in _UNSENT or field == "sessions_revoked":
            continue
        if value is None:
            if field in _REQUIRED_TEXT:
                form[field] = ""
            continue
        form[field] = value
    result = client.post(CONFIG_ENDPOINT, json={**form, **OPEN_FIELDS})
    result.raise_for_status()
    if not signups_open(_admin_config(client.get(CONFIG_ENDPOINT))):
        raise RuntimeError("Open WebUI did not retain the sign-up configuration")
    return FilterSync.UPDATED


def sync_open_signups(
    data_dir: Path,
    base_url: str,
    *,
    client_factory: Callable[[str, str], httpx.Client] | None = None,
) -> FilterSyncReport:
    token = local_admin_token(data_dir)
    if token is None:
        return FilterSyncReport(FilterSync.NO_ADMIN, "Open WebUI has no administrator yet")
    factory = client_factory or (
        lambda url, bearer: httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {bearer}"}, timeout=30.0
        )
    )
    try:
        with factory(base_url, token) as client:
            outcome = open_signups(client)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return FilterSyncReport(FilterSync.UNAVAILABLE, f"sign-ups: {type(exc).__name__}")
    return FilterSyncReport(outcome, f"sign-ups: {outcome.value}")


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def ensure_cloudflared(
    tools: Path,
    *,
    fetch: Callable[[str], Iterable[bytes]] | None = None,
) -> Path:
    """The pinned binary under ``tools``, downloaded once and checked by SHA-256."""
    target = tools / f"cloudflared-{CLOUDFLARED_VERSION}"
    if target.is_file() and _digest(target) == CLOUDFLARED_SHA256:
        return target
    tools.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")

    def download(url: str) -> Iterable[bytes]:
        with urllib.request.urlopen(url, timeout=120) as response:
            yield from iter(lambda: response.read(1 << 20), b"")

    with partial.open("wb") as handle:
        for block in (fetch or download)(CLOUDFLARED_URL):
            handle.write(block)
    if _digest(partial) != CLOUDFLARED_SHA256:
        partial.unlink()
        raise RuntimeError("cloudflared download does not match the pinned SHA-256")
    partial.chmod(0o755)
    partial.replace(target)
    return target


def tunnel_link(line: str) -> str | None:
    match = _LINK.search(line)
    return match.group(0) if match else None


def banner(link: str) -> str:
    rule = "=" * (len(link) + 8)
    return (
        f"\n{rule}\n    {link}\n{rule}\n"
        "Send this link. The guest opens it, clicks Sign up and uses the chat.\n"
        "Keep this terminal open: Ctrl+C ends the link.\n"
    )


def run_tunnel(binary: Path, base_url: str, *, out=sys.stdout) -> int:
    """Relay cloudflared's log, announcing the public link once it appears."""
    process = subprocess.Popen(
        # HTTP/2 over TCP 443: rented pods often drop the UDP that the default QUIC needs.
        [str(binary), "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", base_url],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    announced = False
    try:
        for line in process.stdout or ():
            out.write(line)
            link = None if announced else tunnel_link(line)
            if link:
                out.write(banner(link))
                announced = True
            out.flush()
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait(timeout=15)
        out.write("Link ended.\n")
        return 0


def _answers(base_url: str) -> bool:
    try:
        return httpx.get(f"{base_url}/health", timeout=5.0).status_code == 200
    except httpx.HTTPError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    args = parser.parse_args(argv)
    layout = StackLayout.from_root(args.root)
    try:
        profile = bench_profile(args.profile)
    except BenchProfileError as error:
        print(str(error), file=sys.stderr)
        return 1
    base_url = StackEndpoints.from_profile(profile).open_webui
    if not _answers(base_url):
        print(
            f"Open WebUI is not answering on {base_url}. Start the services first.",
            file=sys.stderr,
        )
        return 1
    report = sync_open_signups(layout.open_webui_data, base_url)
    if report.outcome == FilterSync.NO_ADMIN:
        print(
            "Create your own administrator account first, through your SSH tunnel, "
            "then run share again.",
            file=sys.stderr,
        )
        return 1
    if report.outcome not in (FilterSync.UPDATED, FilterSync.UNCHANGED):
        print(f"Could not open sign-ups ({report.detail}).", file=sys.stderr)
        return 1
    print(f"Sign-ups open as ordinary users ({report.outcome.value}).", flush=True)
    binary = ensure_cloudflared(layout.root / "tools")
    return run_tunnel(binary, base_url)


if __name__ == "__main__":
    raise SystemExit(main())
