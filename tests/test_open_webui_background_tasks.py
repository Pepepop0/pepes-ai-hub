"""Switching Open WebUI 0.12.0's follow-up and tag generation off, title generation kept."""

from __future__ import annotations

import json

import httpx
import pytest

from local_ai_stack import open_webui_background_tasks as module
from local_ai_stack.installation import detect_repo_root
from local_ai_stack.open_webui_background_tasks import (
    CONFIG_ENDPOINT,
    configure_background_tasks,
    sync_background_tasks,
)
from local_ai_stack.open_webui_filters import FilterSync
from local_ai_stack.profiles import QUIET_BACKGROUND_TASKS_RUNTIME_KEY, load_profiles


def defaults() -> dict[str, object]:
    """Open WebUI 0.12.0's GET /api/v1/tasks/config on a fresh database (every field seeded)."""
    return {
        "TASK_MODEL": "",
        "TASK_MODEL_EXTERNAL": "",
        "TASK_MODEL_PARAMS": {},
        "TITLE_GENERATION_PROMPT_TEMPLATE": "",
        "IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE": "",
        "ENABLE_AUTOCOMPLETE_GENERATION": False,
        "AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH": -1,
        "AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE": "",
        "TAGS_GENERATION_PROMPT_TEMPLATE": "",
        "FOLLOW_UP_GENERATION_PROMPT_TEMPLATE": "",
        "ENABLE_FOLLOW_UP_GENERATION": True,
        "ENABLE_TAGS_GENERATION": True,
        "ENABLE_TITLE_GENERATION": True,
        "ENABLE_SEARCH_QUERY_GENERATION": True,
        "ENABLE_RETRIEVAL_QUERY_GENERATION": True,
        "QUERY_GENERATION_PROMPT_TEMPLATE": "",
        "TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE": "",
        "ENABLE_VOICE_MODE_PROMPT": True,
        "VOICE_MODE_PROMPT_TEMPLATE": "",
    }


class TaskConfig:
    """Open WebUI's /api/v1/tasks/config: the update form requires every field."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = values if values is not None else defaults()
        self.updates: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == CONFIG_ENDPOINT:
            return httpx.Response(200, json=dict(self.values))
        if request.method == "POST" and request.url.path == f"{CONFIG_ENDPOINT}/update":
            body = json.loads(request.content)
            self.updates.append(body)
            if set(body) < set(self.values):
                return httpx.Response(422, json={"detail": "field required"})
            self.values.update(body)
            return httpx.Response(200, json=dict(self.values))
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://owui", transport=httpx.MockTransport(self))


def test_follow_ups_and_tags_are_switched_off_and_everything_else_is_sent_back_as_it_was():
    owui = TaskConfig()
    owui.values["ENABLE_TITLE_GENERATION"] = False  # an administrator's own choice
    owui.values["TITLE_GENERATION_PROMPT_TEMPLATE"] = "my title prompt"
    before = dict(owui.values)
    with owui.client() as client:
        assert configure_background_tasks(client) is FilterSync.UPDATED
    (update,) = owui.updates
    assert update == {
        **before,
        "ENABLE_FOLLOW_UP_GENERATION": False,
        "ENABLE_TAGS_GENERATION": False,
    }
    assert owui.values["ENABLE_TITLE_GENERATION"] is False, "the title switch is untouched"
    assert owui.values["TITLE_GENERATION_PROMPT_TEMPLATE"] == "my title prompt"


def test_title_generation_left_on_stays_on():
    owui = TaskConfig()
    with owui.client() as client:
        configure_background_tasks(client)
    assert owui.values["ENABLE_TITLE_GENERATION"] is True
    assert owui.values["ENABLE_FOLLOW_UP_GENERATION"] is False
    assert owui.values["ENABLE_TAGS_GENERATION"] is False


def test_a_second_run_finds_them_off_and_writes_nothing():
    owui = TaskConfig()
    with owui.client() as client:
        assert configure_background_tasks(client) is FilterSync.UPDATED
        assert configure_background_tasks(client) is FilterSync.UNCHANGED
    assert len(owui.updates) == 1


def test_one_switch_turned_back_on_is_switched_off_again():
    owui = TaskConfig({**defaults(), "ENABLE_FOLLOW_UP_GENERATION": False})
    with owui.client() as client:
        assert configure_background_tasks(client) is FilterSync.UPDATED
    assert owui.values["ENABLE_TAGS_GENERATION"] is False


def test_a_retention_failure_is_an_error_not_a_silent_success():
    class Forgetful(TaskConfig):
        def __call__(self, request):
            response = super().__call__(request)
            if request.method == "POST":
                self.values["ENABLE_TAGS_GENERATION"] = True
            return response

    with Forgetful().client() as client, pytest.raises(RuntimeError, match="did not retain"):
        configure_background_tasks(client)


def test_the_sync_without_an_administrator_waits_and_an_unreachable_instance_never_raises(
    tmp_path, monkeypatch
):
    waiting = sync_background_tasks(tmp_path, "http://owui")
    assert waiting.outcome is FilterSync.NO_ADMIN

    monkeypatch.setattr(module, "local_admin_token", lambda _dir: "token")

    def refuse(_url: str, _bearer: str) -> httpx.Client:
        return httpx.Client(
            base_url="http://owui",
            transport=httpx.MockTransport(
                lambda _r: (_ for _ in ()).throw(httpx.ConnectError("refused"))
            ),
        )

    failed = sync_background_tasks(tmp_path, "http://owui", client_factory=refuse)
    assert failed.outcome is FilterSync.UNAVAILABLE and "ConnectError" in failed.detail


def test_the_sync_goes_through_the_administrators_client(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "local_admin_token", lambda _dir: "token")
    owui = TaskConfig()
    seen = []

    def factory(url: str, bearer: str) -> httpx.Client:
        seen.append((url, bearer))
        return owui.client()

    first = sync_background_tasks(tmp_path, "http://owui", client_factory=factory)
    second = sync_background_tasks(tmp_path, "http://owui", client_factory=factory)
    assert (first.outcome, second.outcome) == (FilterSync.UPDATED, FilterSync.UNCHANGED)
    assert seen == [("http://owui", "token")] * 2


def test_only_capella_opts_in():
    enabled = {
        profile.id
        for profile in load_profiles(detect_repo_root() / "profiles")
        if profile.runtime.get(QUIET_BACKGROUND_TASKS_RUNTIME_KEY)
    }
    assert enabled == {"capella"}
