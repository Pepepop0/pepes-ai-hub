from pathlib import Path

import httpx
import pytest

from local_ai_stack import open_webui_image_filter
from local_ai_stack.open_webui_filters import FILTER_ID, install_image_chat_filter


def test_deployment_is_verified_idempotent_and_only_updates_its_own_function():
    records = {}
    writes = []

    def api(request):
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(
                200, json=list(records.values()) if path.endswith("/list") else records[FILTER_ID]
            )
        writes.append(path)
        if path.endswith(("/create", "/update")):
            import json

            records[FILTER_ID] = {**json.loads(request.content), "type": "filter"}
        elif path.endswith("/toggle/global"):
            records[FILTER_ID]["is_global"] = True
        elif path.endswith("/toggle"):
            records[FILTER_ID]["is_active"] = True
        else:
            pytest.fail(f"Unexpected API mutation: {path}")
        return httpx.Response(200, json=records[FILTER_ID])

    with httpx.Client(
        base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(api)
    ) as client:
        assert install_image_chat_filter(client)
        assert len(writes) == 3
        assert records[FILTER_ID]["content"] == Path(open_webui_image_filter.__file__).read_text(
            encoding="utf-8"
        )
        assert not install_image_chat_filter(client)
        assert len(writes) == 3
        records[FILTER_ID]["meta"]["description"] = "user-owned function"
        with pytest.raises(ValueError, match="already owned"):
            install_image_chat_filter(client)
        assert len(writes) == 3


def test_auth_failure_is_never_treated_as_missing_function():
    methods = []

    def unauthorized(request):
        methods.append(request.method)
        return httpx.Response(401)

    with (
        httpx.Client(
            base_url="http://127.0.0.1", transport=httpx.MockTransport(unauthorized)
        ) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        install_image_chat_filter(client)
    assert methods == ["GET"]
