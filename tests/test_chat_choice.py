"""Choosing, importing and removing the chat model of an installation."""

from __future__ import annotations

import shutil

import pytest
from test_installation_switching import (
    PROJECT_ROOT,
    RecordingBackend,
    World,
    custom,
    failed,
    install,
    succeeded,
)

from local_ai_stack.chat_choice import (
    ChatChoice,
    ChatChoiceError,
    ImportedModel,
    choice_path,
    effective_profile,
    parse_reference,
    read_choice,
    with_imports,
    write_choice,
)
from local_ai_stack.hub_application import create_hub_application
from local_ai_stack.installation import resolve_installation
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.model_catalog import load_model_catalog, model_catalog
from local_ai_stack.models import ModelId, ModelProvider
from local_ai_stack.profiles import CHAT_PRIMARY_ROLE, load_profiles
from local_ai_stack.tui.application import HubError

PROFILES = {profile.id: profile for profile in load_profiles(PROJECT_ROOT / "profiles")}
ALTERNATIVE_12GB = "ollama/qwen3_5_9b"


# --- what a person may paste --------------------------------------------------------


@pytest.mark.parametrize(
    "pasted,reference",
    [
        ("someone/chat-model:8b", "someone/chat-model:8b"),
        ("  ollama run someone/chat-model:8b ", "someone/chat-model:8b"),
        ("ollama pull dolphin3:8b", "dolphin3:8b"),
        ("https://ollama.com/library/dolphin3:8b", "dolphin3:8b"),
        ("https://ollama.com/someone/chat-model:8b", "someone/chat-model:8b"),
        ("https://huggingface.co/someone/Model-GGUF:Q4_K_M", "hf.co/someone/Model-GGUF:Q4_K_M"),
        ("hf.co/someone/Model-GGUF:Q5_K_M", "hf.co/someone/Model-GGUF:Q5_K_M"),
    ],
)
def test_pasted_names_become_the_reference_ollama_pulls(pasted, reference):
    assert parse_reference(pasted) == reference


@pytest.mark.parametrize(
    "pasted",
    ["someone/chat-model", "dolphin3:latest", "", "rm -rf /:8b", "a b:c", "../x:1"],
)
def test_names_that_would_not_mean_one_model_are_refused(pasted):
    with pytest.raises(ChatChoiceError):
        parse_reference(pasted)


# --- what the public profiles offer ------------------------------------------------


def test_no_public_profile_offers_an_uncensored_chat_model(tmp_path):
    """Base files only: an installed pack's overlays and manifest extras are left out."""
    (tmp_path / "profiles").mkdir()
    (tmp_path / "manifests").mkdir()
    for path in (PROJECT_ROOT / "profiles").glob("*.yaml"):
        if not path.name.endswith(".overlay.yaml"):
            shutil.copyfile(path, tmp_path / "profiles" / path.name)
    shutil.copyfile(PROJECT_ROOT / "manifests/models.yaml", tmp_path / "manifests/models.yaml")
    catalog = load_model_catalog(tmp_path / "manifests/models.yaml")
    assert not any(spec.uncensored for spec in catalog.for_provider(ModelProvider.OLLAMA))
    profiles = load_profiles(tmp_path / "profiles")
    assert {profile.id for profile in profiles} >= {"pepe", "pepe-8gb", "capella"}
    for profile in profiles:
        chat = profile.models.get("chat", {})
        for model in [*chat.get("primary", "").split(), *chat.get("alternatives", ())]:
            assert not catalog.require(ModelId(model)).uncensored, (profile.id, model)


# --- the profile an installation runs ----------------------------------------------


def test_an_alternative_choice_replaces_the_chat_role():
    catalog = model_catalog()
    pepe = effective_profile(PROFILES["pepe"], ChatChoice(ModelId(ALTERNATIVE_12GB)), catalog)
    assert str(pepe.model_for(CHAT_PRIMARY_ROLE)) == ALTERNATIVE_12GB
    # Image roles are untouched.
    assert pepe.model_for("image.diffusion") == PROFILES["pepe"].model_for("image.diffusion")


def test_a_profile_parameter_wins_over_the_models_own():
    catalog = model_catalog()
    capella = effective_profile(
        PROFILES["capella"], ChatChoice(ModelId("ollama/qwen3_5_27b")), catalog
    )
    assert capella.chat_params == {"think": False, "num_ctx": 16384}


def test_a_choice_the_profile_does_not_offer_is_ignored():
    catalog = model_catalog()
    stranger = ChatChoice(ModelId("ollama/qwen3_5_27b"))  # Capella's, not Pepe's
    assert effective_profile(PROFILES["pepe"], stranger, catalog) is PROFILES["pepe"]


def test_an_imported_model_resolves_only_for_the_installation_that_imported_it():
    item = ImportedModel("hf.co/someone/Model-GGUF:Q4_K_M")
    choice = ChatChoice(item.id, (item,))
    catalog = with_imports(model_catalog(), choice)
    pepe = effective_profile(PROFILES["pepe"], choice, catalog)
    assert pepe.model_for(CHAT_PRIMARY_ROLE) == item.id
    assert catalog.get(item.id).source.reference == "hf.co/someone/Model-GGUF:Q4_K_M"
    assert model_catalog().get(item.id) is None


def test_a_choice_survives_a_round_trip_and_the_default_leaves_no_file(tmp_path):
    path = choice_path(tmp_path)
    item = ImportedModel("dolphin3:8b")
    write_choice(path, ChatChoice(ModelId(ALTERNATIVE_12GB), (item,)))
    assert read_choice(path) == ChatChoice(ModelId(ALTERNATIVE_12GB), (item,))
    write_choice(path, ChatChoice())
    assert not path.exists()
    path.write_text("not: [valid", encoding="utf-8")
    assert read_choice(path) == ChatChoice()


# --- the Hub, with a backend that runs the real choice logic -----------------------


class ChatBackend(RecordingBackend):
    """The recording stub, answering chat questions the way StackApplication does."""

    def __init__(self, installation, world) -> None:
        super().__init__(installation, world)
        self.layout = installation.layout
        self.deleted: list[str] = []
        self.repaired_with: list[str] = []

    def chat_choice(self):
        return read_choice(choice_path(self.layout.state))

    def catalog(self):
        return with_imports(model_catalog(), self.chat_choice())

    def profile(self, profile_id):
        return effective_profile(PROFILES[profile_id], self.chat_choice(), self.catalog())

    def repair(self, operation_id, profile_id, *, emit=None):
        self.repaired_with.append(str(self.profile(profile_id).model_for(CHAT_PRIMARY_ROLE)))
        if self.root in self.world.failing:
            return failed(operation_id, "the chat model could not be downloaded")
        return succeeded(operation_id)

    def delete_chat_model(self, profile_id, reference):
        self.deleted.append(reference)


@pytest.fixture
def chat_hub(tmp_path):
    world = World()
    backends: list[ChatBackend] = []

    def factory(installation):
        backend = ChatBackend(installation, world)
        backends.append(backend)
        world.backends[installation.root] = backend
        return backend

    adapter = create_hub_application(
        PROJECT_ROOT,
        resolve=lambda location: resolve_installation(
            location, repo_root=tmp_path / "repo", env={ENV_ROOT: str(tmp_path / "default")}
        ),
        backend_factory=factory,
    )
    root = tmp_path / "pepe"
    install(adapter, custom(root))
    yield adapter, world, root
    adapter.close()


def selected(adapter):
    return adapter.chat_models()["selected"]


def test_switching_to_an_alternative_repairs_with_it(chat_hub):
    adapter, world, root = chat_hub
    view = adapter.chat_models()
    assert ALTERNATIVE_12GB in [item["id"] for item in view["options"]]
    assert view["selected"] == view["default"] == "ollama/hermes3_8b"

    adapter.choose_chat_model(ALTERNATIVE_12GB)
    adapter.wait(10)

    assert world.backends[root].repaired_with == [ALTERNATIVE_12GB]
    assert selected(adapter) == ALTERNATIVE_12GB
    adapter.choose_chat_model("ollama/hermes3_8b")
    adapter.wait(10)
    assert selected(adapter) == "ollama/hermes3_8b"
    assert not choice_path(world.backends[root].layout.state).exists()


def test_a_failed_download_puts_the_previous_model_back(chat_hub):
    adapter, world, root = chat_hub
    world.failing.add(root)
    adapter.choose_chat_model(ALTERNATIVE_12GB)
    adapter.wait(10)
    assert adapter.current_operation().last_event.state.value == "failed"
    assert selected(adapter) == "ollama/hermes3_8b"


def test_an_import_is_used_and_can_only_be_removed_once_not_in_use(chat_hub):
    adapter, world, root = chat_hub
    adapter.import_chat_model("ollama run someone/chat-model:8b")
    adapter.wait(10)
    imported = next(item for item in adapter.chat_models()["options"] if item["imported"])
    assert imported["reference"] == "someone/chat-model:8b"
    assert imported["selected"]

    with pytest.raises(HubError):
        adapter.remove_imported_chat_model(imported["id"])
    adapter.choose_chat_model("ollama/hermes3_8b")
    adapter.wait(10)
    adapter.remove_imported_chat_model(imported["id"])

    assert world.backends[root].deleted == ["someone/chat-model:8b"]
    assert not any(item["imported"] for item in adapter.chat_models()["options"])


def test_a_failed_import_leaves_no_trace(chat_hub):
    adapter, world, root = chat_hub
    world.failing.add(root)
    adapter.import_chat_model("dolphin3:8b")
    adapter.wait(10)
    view = adapter.chat_models()
    assert view["selected"] == "ollama/hermes3_8b"
    assert not any(item["imported"] for item in view["options"])


@pytest.mark.parametrize("model_id", ["ollama/qwen3_5_27b", "not-an-id", "ollama/hermes3_8b"])
def test_models_the_installation_cannot_switch_to_are_refused(chat_hub, model_id):
    adapter, _, _ = chat_hub
    with pytest.raises(HubError):
        adapter.choose_chat_model(model_id)
