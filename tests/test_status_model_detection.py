"""Status/configuration reuse detections only within one controlled read pass."""

from collections import Counter

import pytest
from first_run_support import CAPELLA, controlled_stack

from local_ai_stack import model_installer
from local_ai_stack.models import FileModelSource
from local_ai_stack.operation_contracts import OperationState


@pytest.fixture
def installed_stack():
    stack = controlled_stack()
    try:
        result = stack.app.first_run("install", CAPELLA)
        assert result.state is OperationState.SUCCEEDED
        yield stack
    finally:
        stack.release()


@pytest.mark.parametrize("operation", ["status", "stack_configuration"])
def test_invalid_model_is_hashed_once_per_read_pass(installed_stack, monkeypatch, operation):
    app = installed_stack.app
    profile = app.profile(CAPELLA)
    installer = app.model_installer(profile)
    spec = next(
        spec
        for spec in app.catalog().specs.values()
        if isinstance(spec.source, FileModelSource)
        and installer.paths.destination_for(spec).is_file()
    )
    destination = installer.paths.destination_for(spec)
    original = destination.read_bytes()
    destination.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    calls = Counter()
    real_hasher = model_installer.sha256_of

    def counted(path):
        calls[path] += 1
        return real_hasher(path)

    monkeypatch.setattr(model_installer, "sha256_of", counted)

    def read():
        if operation == "status":
            assert not app.status(CAPELLA).installation_ready
        else:
            result = app.stack_configuration(profile)
            assert result is None or not result.ok

    read()
    assert calls == {destination: 1}
    read()
    assert calls == {destination: 2}, "a later operation must observe the file again"


def test_verified_status_and_second_run_never_hash(installed_stack, monkeypatch):
    def forbidden(_path):
        pytest.fail("unchanged verified weights must not be read in full")

    monkeypatch.setattr(model_installer, "sha256_of", forbidden)
    app = installed_stack.app
    assert app.status(CAPELLA).installation_ready
    assert app.services_snapshot(CAPELLA).services
    result = app.first_run("second", CAPELLA)
    assert result.state is OperationState.SUCCEEDED
    assert app.last_first_run.did_nothing
