from pathlib import Path

from local_ai_stack.assets import ENV_ASSETS_ROOT, assets_directory


def test_assets_use_the_product_environment_variable(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    monkeypatch.setenv(ENV_ASSETS_ROOT, str(root))
    assert ENV_ASSETS_ROOT == "PEPITOS_AI_HUB_ASSETS"
    assert assets_directory() == root


def test_legacy_assets_environment_variable_is_not_an_alias(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    monkeypatch.delenv(ENV_ASSETS_ROOT, raising=False)
    monkeypatch.setenv("LOCAL_AI_STACK_ASSETS", str(legacy))
    assert assets_directory() != legacy
    assert assets_directory() == Path(__file__).resolve().parents[1] / "assets"
