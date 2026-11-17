"""Consumer-visible security boundaries over HTTP and the real Hub adapter."""

from __future__ import annotations

import http.client
import json
import threading

import pytest
from test_installation_switching import World, custom, hub, install

from local_ai_stack.desktop.api import DesktopController, DesktopError
from local_ai_stack.desktop.server import MAX_BODY_BYTES, DesktopServer


@pytest.fixture
def desktop(tmp_path):
    world = World()
    app = hub(tmp_path, world)
    root = tmp_path / "installed"
    install(app, custom(root))
    opened = []
    controller = DesktopController(app, folder_opener=opened.append, url_opener=opened.append)
    yield controller, app, world, root, opened
    controller.close()


@pytest.fixture
def server(desktop, tmp_path):
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "index.html").write_text("<html>Hub</html>", encoding="utf-8")
    local = DesktopServer(desktop[0], assets)
    local.start()
    yield local
    local.close()


def request(server, method="GET", path="/api/state", *, auth=True, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    merged = {"Authorization": f"Bearer {server.token}"} if auth else {}
    merged.update(headers or {})
    connection.request(method, path, body=body, headers=merged)
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


@pytest.mark.parametrize(
    "headers,auth,code",
    [
        ({}, False, "unauthorized"),
        ({"Authorization": "Bearer wrong"}, True, "unauthorized"),
        ({"Host": "attacker.example"}, True, "forbidden_host"),
        ({"Origin": "https://attacker.example"}, True, "forbidden_origin"),
        ({"Origin": "null"}, True, "forbidden_origin"),
        ({"Sec-Fetch-Site": "cross-site"}, True, "forbidden_origin"),
    ],
)
def test_requests_cannot_cross_session_host_or_origin(server, headers, auth, code):
    status, data, returned_headers = request(server, headers=headers, auth=auth)
    assert status in {401, 403}
    assert json.loads(data)["code"] == code
    assert "Access-Control-Allow-Origin" not in returned_headers
    assert server.token.encode() not in data


def test_static_assets_need_no_token_but_cannot_escape_bundle(server, tmp_path):
    secret = tmp_path / "private.txt"
    secret.write_text("not public", encoding="utf-8")
    status, _, headers = request(server, path="/", auth=False)
    assert status == 200
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    for path in ("/../private.txt", "/%2e%2e/private.txt", "/..%5cprivate.txt"):
        status, data, _ = request(server, path=path, auth=False)
        assert status == 403
        assert b"not public" not in data


def test_reloading_a_selected_page_serves_the_shell_but_the_api_rejects_queries(server):
    status, data, _ = request(server, path="/?page=installations", auth=False)
    assert (status, data) == (200, b"<html>Hub</html>")
    status, data, _ = request(server, path="/api/state?page=installations")
    assert status == 400
    assert json.loads(data)["code"] == "invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "__getattribute__", "args": {}},
        {"command": "start", "args": {"root": "foreign"}},
        {
            "command": "delete",
            "args": {"root": "foreign", "confirmation": "foreign", "stop": "false"},
        },
        {"command": "start", "args": [], "extra": True},
    ],
)
def test_dispatch_rejects_unknown_methods_and_arguments(server, payload):
    status, body, _ = request(
        server,
        "POST",
        "/api/command",
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload),
    )
    assert status == 400
    assert json.loads(body)["ok"] is False


def test_json_body_is_bounded_before_reading(server):
    status, data, _ = request(
        server,
        "POST",
        "/api/command",
        body=b"",
        headers={"Content-Type": "application/json", "Content-Length": str(MAX_BODY_BYTES + 1)},
    )
    assert status == 413
    assert json.loads(data)["code"] == "payload_too_large"


def test_delete_requires_exact_confirmation_and_explicit_stop(desktop):
    controller, app, world, root, _ = desktop
    world.running.add(root)
    for confirmation, stop in ((root.name, True), (str(root), False)):
        with pytest.raises(DesktopError):
            controller.command(
                "delete", {"root": str(root), "confirmation": confirmation, "stop": stop}
            )
        assert root.exists()
        assert "stop_stack" not in world.commands_on(root)
    result = controller.command(
        "delete", {"root": str(root), "confirmation": str(root), "stop": True}
    )
    assert result["operation_id"]
    app.wait(10)
    assert not root.exists()
    assert app.active_root() is None


def test_foreign_folder_and_arbitrary_urls_are_never_opened_or_deleted(desktop, tmp_path):
    controller, _, _, _, opened = desktop
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep", encoding="utf-8")
    for command, args in (
        ("open_folder", {"root": str(foreign), "kind": "root"}),
        ("delete", {"root": str(foreign), "confirmation": str(foreign)}),
        ("open_url", {"url": "file:///C:/Windows/System32/cmd.exe"}),
        ("open_url", {"url": "https://huggingface.co.attacker.example/settings/tokens"}),
    ):
        with pytest.raises(DesktopError):
            controller.command(command, args)
    assert not opened
    assert (foreign / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_only_existing_approved_subfolders_can_open(desktop):
    controller, _, _, root, opened = desktop
    controller.command("open_folder", {"root": str(root), "kind": "root"})
    assert opened == [root]
    with pytest.raises(DesktopError):
        controller.command("open_folder", {"root": str(root), "kind": "../elsewhere"})
    with pytest.raises(DesktopError):
        controller.command("open_folder", {"root": str(root), "kind": "logs"})
    assert opened == [root]


def test_redirected_model_directory_is_not_opened(desktop, tmp_path):
    controller, _, _, root, opened = desktop
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    try:
        (root / "models").symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks requires local OS permission")
    with pytest.raises(DesktopError) as error:
        controller.command("open_folder", {"root": str(root), "kind": "models"})
    assert error.value.code == "unsafe_path"
    assert not opened


def test_close_and_mutations_are_refused_while_a_real_hub_worker_runs(desktop):
    controller, app, world, root, _ = desktop
    entered, release = threading.Event(), threading.Event()
    original = world.backends[root].stop_stack

    def blocked_stop(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    world.backends[root].stop_stack = blocked_stop
    controller.command("stop", {})
    try:
        assert entered.wait(5)
        assert not controller.prepare_close()
        with pytest.raises(DesktopError) as error:
            controller.command("forget", {"root": str(root)})
        assert error.value.code == "busy"
        assert controller.state()["busy"]
    finally:
        release.set()
        app.wait(10)
    assert controller.prepare_close()
    with pytest.raises(DesktopError) as error:
        controller.command("start", {})
    assert error.value.code == "closing"


def test_stale_conflict_decision_and_cancel_preserve_active_installation(desktop, tmp_path):
    controller, app, world, original, _ = desktop
    candidate = tmp_path / "candidate"
    world.conflicting.add(candidate)
    with pytest.raises(DesktopError) as error:
        controller.command(
            "install",
            {
                "profile_id": "pepe-8gb",
                "mode": "custom",
                "custom_root": str(candidate),
                "create_subfolder": False,
            },
        )
    assert error.value.code == "conflict_required"
    assert error.value.details["available_policies"] == ["abort"]
    with pytest.raises(DesktopError):
        controller.command("resolve_conflicts", {"preflight_id": "stale", "policy": "abort"})
    assert app.active_root() == original
    controller.command("cancel_pending", {})
    assert app.current_conflict_preflight() is None


def test_register_refuses_unrecognized_folder_without_rebinding(desktop, tmp_path):
    controller, app, _, original, _ = desktop
    foreign = tmp_path / "not-an-installation"
    foreign.mkdir()
    with pytest.raises(DesktopError) as error:
        controller.command("register", {"root": str(foreign)})
    assert error.value.code == "refused"
    assert app.active_root() == original


def test_hf_failure_never_echoes_submitted_credential(server, monkeypatch):
    from local_ai_stack import huggingface_auth

    secret = "hf_sensitive_credential_do_not_echo"

    def rejected(token):
        raise huggingface_auth.HuggingFaceAuthError(f"upstream rejected {token}")

    monkeypatch.setattr(huggingface_auth, "authenticate_with_token", rejected)
    status, body, _ = request(
        server,
        "POST",
        "/api/command",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"command": "hf_login", "args": {"token": secret}}),
    )
    assert status == 400
    assert json.loads(body)["code"] == "hf_auth"
    assert secret.encode() not in body
    status, body, _ = request(server)
    assert status == 200
    assert secret.encode() not in body


def test_civitai_failure_never_echoes_submitted_credential(server, monkeypatch):
    from local_ai_stack import civitai_auth

    secret = "civitai_sensitive_credential_do_not_echo"

    def rejected(token):
        raise civitai_auth.CivitaiAuthError(f"upstream rejected {token}")

    monkeypatch.setattr(civitai_auth, "authenticate_with_token", rejected)
    status, body, _ = request(
        server,
        "POST",
        "/api/command",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"command": "civitai_login", "args": {"token": secret}}),
    )
    assert status == 400
    assert json.loads(body)["code"] == "civitai_auth"
    assert secret.encode() not in body
    status, body, _ = request(server)
    assert status == 200
    assert secret.encode() not in body


def test_projection_failure_returns_actionable_json_not_a_traceback(server, desktop, monkeypatch):
    from local_ai_stack.tui.application import HubUnavailable

    def unavailable(*_args, **_kwargs):
        raise HubUnavailable("Não foi possível verificar a instalação. Tente novamente.")

    monkeypatch.setattr(desktop[1], "projections", unavailable)
    status, body, _ = request(server)
    assert status == 503
    payload = json.loads(body)
    assert payload["code"] == "unavailable"
    assert "Tente novamente" in payload["error"]
    assert "Traceback" not in payload["error"]


def command(server, name: str, args: dict | None = None) -> tuple[int, dict]:
    status, body, _ = request(
        server,
        "POST",
        "/api/command",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"command": name, "args": args or {}}),
    )
    payload = json.loads(body)
    return status, payload.get("result", payload)


@pytest.fixture
def web_search_home(tmp_path, monkeypatch):
    from local_ai_stack import web_search_settings

    monkeypatch.setattr(web_search_settings, "_config_directory", lambda: tmp_path)
    monkeypatch.delenv(web_search_settings.BRAVE_KEY_ENV, raising=False)
    monkeypatch.delenv(web_search_settings.UNSPLASH_KEY_ENV, raising=False)
    monkeypatch.delenv(web_search_settings.PEXELS_KEY_ENV, raising=False)
    monkeypatch.setattr(web_search_settings, "protect", lambda data, _label: b"sealed:" + data)
    monkeypatch.setattr(
        web_search_settings, "unprotect", lambda data: data.removeprefix(b"sealed:")
    )
    return web_search_settings


def test_web_search_settings_are_read_and_changed_through_the_command_boundary(
    server, web_search_home
):
    status, state = command(server, "web_search_status")
    assert status == 200 and state == {
        "enabled": True,
        "engine": "duckduckgo",
        "searxng_url": "",
        "brave_configured": False,
        "unsplash_configured": False,
        "pexels_configured": False,
        "brave_source": None,
        "unsplash_source": None,
        "pexels_source": None,
    }
    status, state = command(
        server,
        "web_search_configure",
        {"enabled": False, "searxng_url": "http://localhost:8888"},
    )
    assert status == 200 and state["enabled"] is False and state["engine"] == "searxng"
    # A change that omits the address keeps the one set.
    status, state = command(server, "web_search_configure", {"enabled": True})
    assert state["enabled"] is True and state["searxng_url"].startswith("http://localhost:8888")


def test_web_search_settings_refuse_wrong_types_and_addresses_with_a_clear_message(
    server, web_search_home
):
    status, body = command(server, "web_search_configure", {"enabled": "yes"})
    assert status == 400
    status, body = command(
        server, "web_search_configure", {"enabled": True, "searxng_url": "ftp://x/search"}
    )
    assert status == 400 and body["code"] == "web_search" and "SearXNG" in body["error"]
    status, body = command(server, "web_search_configure", {"enabled": True, "searxng_url": 7})
    assert status == 400


def test_a_brave_failure_never_echoes_the_submitted_key(server, web_search_home, monkeypatch):
    secret = "brave_sensitive_key_do_not_echo"

    def rejected(key, **_kwargs):
        raise web_search_home.WebSearchSettingsError("O Brave Search recusou a chave.")

    monkeypatch.setattr(web_search_home, "check_brave_key", rejected)
    status, body, _ = request(
        server,
        "POST",
        "/api/command",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"command": "brave_login", "args": {"token": secret}}),
    )
    assert status == 400 and json.loads(body)["code"] == "web_search"
    assert secret.encode() not in body
    status, body = command(server, "web_search_status")
    assert body["brave_configured"] is False and secret not in json.dumps(body)


def test_a_brave_key_is_kept_sealed_and_removed_on_logout(server, web_search_home, monkeypatch):
    monkeypatch.setattr(web_search_home, "check_brave_key", lambda key, **_kwargs: None)
    status, state = command(server, "brave_login", {"token": "brave-secret"})
    assert status == 200 and state["brave_configured"] is True and state["engine"] == "brave"
    assert "brave-secret" not in json.dumps(state)
    status, state = command(server, "brave_logout")
    assert state["brave_configured"] is False and state["engine"] == "duckduckgo"


@pytest.mark.parametrize("provider", ["unsplash", "pexels"])
def test_an_image_provider_key_crosses_the_command_boundary_without_echo_and_can_be_removed(
    server, web_search_home, monkeypatch, provider
):
    monkeypatch.setattr(web_search_home, "check_image_key", lambda *args, **kwargs: None)
    status, state = command(
        server,
        "image_search_login",
        {"provider": provider, "token": "picture-secret"},
    )
    assert status == 200 and state[f"{provider}_configured"] is True
    assert "picture-secret" not in json.dumps(state)

    status, state = command(server, "image_search_logout", {"provider": provider})
    assert status == 200 and state[f"{provider}_configured"] is False


def test_an_unknown_image_provider_does_not_echo_the_submitted_key(server, web_search_home):
    secret = "unknown-provider-sensitive-key"
    status, body, _headers = request(
        server,
        "POST",
        "/api/command",
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {
                "command": "image_search_login",
                "args": {"provider": "unknown", "token": secret},
            }
        ),
    )
    assert status == 400 and json.loads(body)["code"] == "web_search"
    assert secret.encode() not in body
