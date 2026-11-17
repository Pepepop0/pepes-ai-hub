from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from test_installation_switching import World, hub

from local_ai_stack.desktop.api import DesktopController, DesktopError
from local_ai_stack.prerequisites import (
    GIT_DOWNLOAD_URL,
    GIT_PACKAGE_ID,
    INSTALL_TIMEOUT_SECONDS,
    GitInstallResult,
    PrerequisiteInstallError,
    install_git_with_winget,
)
from local_ai_stack.runner import CommandResult


class ControlledRunner:
    def __init__(
        self,
        *,
        binaries: Mapping[str, str] | None = None,
        result: CommandResult | None = None,
    ) -> None:
        self.binaries = dict(binaries or {})
        self.result = result or CommandResult(("winget",), 0)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def which(self, name: str, extra_env=None) -> str | None:
        return self.binaries.get(name)

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(argv)
        self.calls.append((items, kwargs))
        return self.result


def test_existing_git_never_runs_a_package_manager():
    runner = ControlledRunner(binaries={"git": r"C:\Program Files\Git\cmd\git.exe"})

    result = install_git_with_winget(runner)

    assert result == GitInstallResult(True, False, False, "O Git já está disponível neste sistema.")
    assert runner.calls == []


def test_installer_uses_only_the_fixed_official_winget_package():
    winget = r"C:\Windows\winget.exe"
    runner = ControlledRunner(binaries={"winget": winget})

    result = install_git_with_winget(runner)

    assert result.installed and result.changed and result.restart_required
    assert runner.calls == [
        (
            (
                winget,
                "install",
                "--id",
                GIT_PACKAGE_ID,
                "--exact",
                "--source",
                "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--silent",
                "--disable-interactivity",
            ),
            {"timeout": INSTALL_TIMEOUT_SECONDS, "dry_run": False},
        )
    ]


def test_missing_winget_names_the_manual_fallback_without_running_anything():
    runner = ControlledRunner()

    with pytest.raises(PrerequisiteInstallError) as caught:
        install_git_with_winget(runner)

    assert caught.value.code == "winget_missing"
    assert "Baixar manualmente" in str(caught.value)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (CommandResult(("winget",), -1, timed_out=True), "winget_timeout"),
        (CommandResult(("winget",), 5, stderr="untrusted output"), "winget_failed"),
    ],
)
def test_winget_failures_are_classified_without_echoing_its_output(result, code):
    runner = ControlledRunner(binaries={"winget": r"C:\Windows\winget.exe"}, result=result)

    with pytest.raises(PrerequisiteInstallError) as caught:
        install_git_with_winget(runner)

    assert caught.value.code == code
    assert "untrusted output" not in str(caught.value)


def test_desktop_requires_explicit_consent_and_allows_the_manual_git_page(tmp_path):
    app = hub(tmp_path, World())
    calls = []
    opened = []
    controller = DesktopController(
        app,
        folder_picker=object(),
        url_opener=opened.append,
        git_installer=lambda: (
            calls.append("install") or GitInstallResult(True, True, True, "installed")
        ),
    )
    try:
        with pytest.raises(DesktopError) as caught:
            controller.command("install_git", {"confirmation": "yes"})
        assert caught.value.code == "confirmation_required"
        assert calls == []

        installed = controller.command("install_git", {"confirmation": "install-git"})
        assert installed["restart_required"] is True
        assert calls == ["install"]

        controller.command("open_url", {"url": GIT_DOWNLOAD_URL})
        assert opened == [GIT_DOWNLOAD_URL]
    finally:
        controller.close()
