"""Custom install folder: native picker boundary, recent paths, persistence.

Three layers. The recent-path policy is pure and lives on ``GeneralConfig``.
Persistence runs through the real ``HubApplicationAdapter`` against a stub
backend and a temporary state directory, so a "restart" is a second adapter
reading the same file. The wizard runs through Textual's pilot with a
``FakeFolderPicker``: no COM, no window, but the same worker-thread seam the
Windows dialog uses.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from hub_fakes import FakeFolderPicker, FakeHubApplication
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from test_tui_operations import click, pump, run, text, wizard
from textual.widgets import Button, Input, Select, Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.config import (
    RECENT_CUSTOM_ROOTS_LIMIT,
    ConfigError,
    GeneralConfig,
    InstalledConfig,
    dumps,
    loads,
)
from local_ai_stack.external_conflict_contracts import ConflictHardBlocker, ConflictPreflightReport
from local_ai_stack.folder_picker import (
    NullFolderPicker,
    WindowsFolderPicker,
    _hresult_cancelled,
    _hresult_succeeded,
    default_folder_picker,
    starting_folder,
)
from local_ai_stack.hub_application import create_hub_application
from local_ai_stack.installation import InstallLocationMode, resolve_installation
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.pets import unsafe_characters
from local_ai_stack.tui.application import ConflictResolutionRequired, InstallSelection
from local_ai_stack.tui.dialogs import WelcomeScreen
from local_ai_stack.tui.wizard import (
    BROWSE_BUSY_LABEL,
    BROWSE_LABEL,
    NO_RECENT_TEXT,
    WizardScreen,
    WizardStep,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_TUI = PROJECT_ROOT / "src" / "local_ai_stack" / "tui"


# --- recent-path policy -----------------------------------------------------------------


def roots_after(*attempts: str) -> tuple[str, ...]:
    general = GeneralConfig()
    for root in attempts:
        general = general.remembering_custom_root(root)
    return general.recent_custom_roots


def test_no_recent_paths_by_default():
    assert GeneralConfig().recent_custom_roots == ()


def test_one_recent_path_is_kept():
    assert roots_after(r"C:\AI\Pepito") == (r"C:\AI\Pepito",)


def test_five_recent_paths_are_kept_newest_first():
    attempts = [rf"C:\AI\root{index}" for index in range(5)]
    assert roots_after(*attempts) == tuple(reversed(attempts))


def test_a_sixth_path_evicts_the_oldest():
    attempts = [rf"C:\AI\root{index}" for index in range(6)]
    roots = roots_after(*attempts)
    assert len(roots) == RECENT_CUSTOM_ROOTS_LIMIT == 5
    assert roots[0] == r"C:\AI\root5"
    assert r"C:\AI\root0" not in roots


def test_case_slash_and_trailing_separator_variants_are_one_entry():
    roots = roots_after(r"C:\AI\Pepito", "c:\\ai\\pepito\\", "C:/AI/Pepito")
    assert len(roots) == 1
    # The newest spelling is the one kept, normalized.
    assert roots[0].casefold() == r"c:\ai\pepito"


def test_reselecting_a_path_moves_it_to_the_top():
    roots = roots_after(r"C:\A", r"C:\B", r"C:\C", r"C:\A")
    assert roots == (r"C:\A", r"C:\C", r"C:\B")


def test_recent_paths_round_trip_through_yaml_and_never_exceed_five():
    general = GeneralConfig(recent_custom_roots=tuple(rf"D:\r{index}" for index in range(9)))
    assert len(general.recent_custom_roots) == 5
    config = InstalledConfig("test", general=general)
    assert loads(dumps(config)).general.recent_custom_roots == general.recent_custom_roots


def test_an_older_config_without_recent_paths_still_loads():
    text_without = (
        "schema_version: 1\nprofile: {id: test}\n"
        "installation: {mode: default, root: null}\n"
        "general: {ui: {theme: null, pet: null}}\n"
    )
    assert loads(text_without).general.recent_custom_roots == ()


def test_an_older_custom_config_migrates_its_installed_root_into_history():
    text_without = (
        "schema_version: 1\nprofile: {id: test}\n"
        "installation: {mode: custom, root: 'C:\\Users\\Pedro\\Desktop\\Capellah_gpt'}\n"
        "general: {ui: {theme: null, pet: null}}\n"
    )
    loaded = loads(text_without)
    assert loaded.general.recent_custom_roots == (r"C:\Users\Pedro\Desktop\Capellah_gpt",)
    assert loads(dumps(loaded)).general.recent_custom_roots == loaded.general.recent_custom_roots


@pytest.mark.parametrize("bad", ["not-a-list", [1, 2], ["", "C:\\ok"]])
def test_a_malformed_recent_list_is_rejected(bad):
    with pytest.raises(ConfigError):
        GeneralConfig.from_dict({"recent_custom_roots": bad})


# --- persistence through the real adapter ------------------------------------------------


class StubBackend:
    """Just enough of StackApplication for the adapter to accept an install."""

    def __init__(self, installation, *, outcome: str = "failed", conflict: bool = False) -> None:
        self.installation = installation
        self.outcome = outcome
        self.conflict = conflict
        self.first_runs = 0

    def status(self, profile_id=None):
        return SimpleNamespace(installation_ready=False)

    def facts(self):
        return reference_facts()

    def check_conflicts(self, profile_id):
        if self.conflict:
            return ConflictPreflightReport(
                "blocked-preflight",
                hard_blockers=(ConflictHardBlocker("blocked", "A controlled blocker."),),
            )
        return None

    def current_operation(self):
        return None

    def first_run(self, operation_id, profile_id, *, emit=None):
        # The adapter never inspects this: a first run that ends in failure is
        # indistinguishable, for persistence, from one that succeeds.
        self.first_runs += 1
        return SimpleNamespace(state=self.outcome)

    def close(self) -> None:
        pass


def adapter_for(tmp_path: Path, *, outcome: str = "failed", conflict: bool = False):
    def resolve(location):
        return resolve_installation(
            location, repo_root=tmp_path / "repo", env={ENV_ROOT: str(tmp_path / "default")}
        )

    return create_hub_application(
        PROJECT_ROOT,
        resolve=resolve,
        backend_factory=lambda installation: StubBackend(
            installation, outcome=outcome, conflict=conflict
        ),
    )


def attempt(adapter, selection: InstallSelection) -> None:
    adapter.install(selection)
    adapter.wait(10)


def test_recent_paths_survive_a_restart(tmp_path):
    first = adapter_for(tmp_path)
    try:
        attempt(first, InstallSelection("test", InstallLocationMode.CUSTOM, str(tmp_path / "a")))
        attempt(first, InstallSelection("test", InstallLocationMode.CUSTOM, str(tmp_path / "b")))
    finally:
        first.close()

    restarted = adapter_for(tmp_path)
    try:
        assert restarted.recent_custom_roots() == (str(tmp_path / "b"), str(tmp_path / "a"))
    finally:
        restarted.close()
    persisted = (tmp_path / "default" / "state" / "hub-selection.yaml").read_text(encoding="utf-8")
    assert "recent_custom_roots" in persisted


def test_a_failed_install_still_records_its_root(tmp_path):
    root = tmp_path / "Capellah_gpt"
    adapter = adapter_for(tmp_path, outcome="failed")
    try:
        attempt(adapter, InstallSelection("test", InstallLocationMode.CUSTOM, str(root)))
        assert not adapter.installation().ready
        assert adapter.recent_custom_roots() == (str(root),)
    finally:
        adapter.close()


def test_a_custom_root_cancelled_at_conflict_preflight_is_not_recorded(tmp_path):
    root = tmp_path / "cancelled-before-install"
    adapter = adapter_for(tmp_path, conflict=True)
    try:
        with pytest.raises(ConflictResolutionRequired):
            adapter.install(InstallSelection("test", InstallLocationMode.CUSTOM, str(root)))
        assert adapter.recent_custom_roots() == ()
    finally:
        adapter.close()

    restarted = adapter_for(tmp_path)
    try:
        assert restarted.recent_custom_roots() == ()
    finally:
        restarted.close()


def test_default_and_portable_attempts_never_register_and_keep_history(tmp_path):
    adapter = adapter_for(tmp_path)
    try:
        custom = str(tmp_path / "custom")
        attempt(adapter, InstallSelection("test", InstallLocationMode.CUSTOM, custom))
        attempt(adapter, InstallSelection("test", InstallLocationMode.DEFAULT))
        attempt(adapter, InstallSelection("test", InstallLocationMode.PORTABLE))
        assert adapter.recent_custom_roots() == (custom,)
    finally:
        adapter.close()


def test_the_persisted_history_never_exceeds_five(tmp_path):
    adapter = adapter_for(tmp_path)
    try:
        for index in range(7):
            attempt(
                adapter,
                InstallSelection("test", InstallLocationMode.CUSTOM, str(tmp_path / f"r{index}")),
            )
    finally:
        adapter.close()
    restarted = adapter_for(tmp_path)
    try:
        roots = restarted.recent_custom_roots()
        assert len(roots) == 5
        assert roots[0] == str(tmp_path / "r6")
    finally:
        restarted.close()


# --- the native picker module --------------------------------------------------------------


def test_starting_folder_is_the_path_or_its_nearest_existing_ancestor(tmp_path):
    existing = tmp_path / "exists"
    existing.mkdir()
    assert starting_folder(existing) == existing
    assert starting_folder(existing / "not" / "yet") == existing
    assert starting_folder(None) is None
    assert starting_folder(Path("relative")) is None


def test_null_picker_returns_nothing_and_windows_is_the_default_here():
    assert NullFolderPicker().choose_folder(Path("C:/")) is None
    import sys

    expected = WindowsFolderPicker if sys.platform == "win32" else NullFolderPicker
    assert isinstance(default_folder_picker(), expected)


def test_com_initialization_requires_a_success_hresult_and_cancel_is_explicit():
    assert _hresult_succeeded(0)  # S_OK
    assert _hresult_succeeded(1)  # S_FALSE; still balanced with CoUninitialize
    assert not _hresult_succeeded(-2147417850)  # RPC_E_CHANGED_MODE
    assert _hresult_cancelled(-2147023673)
    assert not _hresult_cancelled(-1)


def test_the_tui_package_never_touches_win32_directly():
    for source in SRC_TUI.rglob("*.py"):
        content = source.read_text(encoding="utf-8")
        assert "ctypes" not in content, source
        assert "folder_picker" not in content, source
        assert "windll" not in content, source


# --- the wizard -------------------------------------------------------------------------------


def hub_with(application: FakeHubApplication | None = None, picker: FakeFolderPicker | None = None):
    fake = application if application is not None else FakeHubApplication()
    app = LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        application=fake,
        open_browser=lambda url: True,
        first_run_wizard=True,
        folder_picker=picker,
    )
    return app, fake


async def to_custom(pilot) -> WizardScreen:
    # The wizard opens once the shell's first projections say nothing is
    # installed; that answer arrives from a worker, not from the constructor.
    await settle(pilot)
    screen = wizard(pilot.app)
    await click(pilot, "#wizard-next")
    await click(pilot, "#wizard-profile-capella")
    await click(pilot, "#wizard-next")
    await click(pilot, "#wizard-mode-custom")
    assert screen.state.step is WizardStep.MODE
    assert screen.state.mode is InstallLocationMode.CUSTOM
    return screen


async def settle(pilot) -> None:
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()
    # A first opening offers the first installation; these tests say yes.
    if isinstance(pilot.app.screen, WelcomeScreen):
        await click(pilot, "#welcome-start")
        await pilot.pause()


SIZE = (110, 50)


def test_the_picker_fills_the_field_validates_it_and_review_shows_it(tmp_path):
    chosen = tmp_path / "Capellah_gpt"
    picker = FakeFolderPicker(result=chosen)

    async def exercise() -> None:
        app, fake = hub_with(picker=picker)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            await click(pilot, "#wizard-custom-browse")
            await settle(pilot)

            assert picker.calls == [None]
            assert screen.query_one("#wizard-custom-root", Input).value == str(chosen)
            assert screen.state.custom_error == ""
            assert screen.state.selection == InstallSelection(
                "capella", InstallLocationMode.CUSTOM, str(chosen)
            )
            await click(pilot, "#wizard-next")
            assert screen.state.step is WizardStep.REVIEW
            review = text(app, "#wizard-step-review")
            assert f"Pasta: {chosen}" in review
            assert "Local: [b]Personalizado[/b]" in review
            assert fake.calls == [], "choosing a folder never starts an install"

    run(exercise)


def test_cancelling_the_picker_keeps_the_previous_path(tmp_path):
    previous = str(tmp_path / "kept")
    picker = FakeFolderPicker(result=None)

    async def exercise() -> None:
        app, _ = hub_with(picker=picker)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            screen.fill_custom_root(previous)
            await pilot.pause()
            await click(pilot, "#wizard-custom-browse")
            await settle(pilot)

            assert picker.calls == [Path(previous)], "a valid typed root is the starting point"
            assert screen.query_one("#wizard-custom-root", Input).value == previous
            assert screen.state.custom_root == previous
            assert not screen.picker_busy
            assert not screen.query_one("#wizard-custom-browse", Button).disabled

    run(exercise)


def test_an_invalid_typed_root_does_not_seed_the_dialog():
    picker = FakeFolderPicker(result=None)

    async def exercise() -> None:
        app, _ = hub_with(picker=picker)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            screen.fill_custom_root("relativo")
            await pilot.pause()
            await click(pilot, "#wizard-custom-browse")
            await settle(pilot)
            assert picker.calls == [None]

    run(exercise)


def test_a_double_click_opens_one_dialog(tmp_path):
    picker = FakeFolderPicker(result=tmp_path / "once")
    picker.hold()

    async def exercise() -> None:
        app, _ = hub_with(picker=picker)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            await click(pilot, "#wizard-custom-browse")
            assert await asyncio.to_thread(picker.entered.wait, 5)
            # A second press, both through the button and the method.
            screen.query_one("#wizard-custom-browse", Button).press()
            screen.request_folder()
            await pilot.pause()
            picker.release()
            await settle(pilot)
            assert len(picker.calls) == 1
            assert screen.state.custom_root == str(tmp_path / "once")

    try:
        run(exercise)
    finally:
        picker.release()


def test_the_event_loop_keeps_running_while_the_dialog_is_open(tmp_path):
    picker = FakeFolderPicker(result=None)
    picker.hold()
    typed = str(tmp_path / "typed-meanwhile")

    async def exercise() -> None:
        app, _ = hub_with(picker=picker)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            await click(pilot, "#wizard-custom-browse")
            assert await asyncio.to_thread(picker.entered.wait, 5)

            browse = screen.query_one("#wizard-custom-browse", Button)
            assert screen.picker_busy and browse.disabled
            assert str(browse.label) == BROWSE_BUSY_LABEL
            assert screen.query_one("#wizard-next", Button).disabled
            assert picker.threads[0] != threading.main_thread().name

            # Escape belongs to the dialog, not to the wizard.
            await pilot.press("escape")
            assert screen.state.step is WizardStep.MODE

            # The loop still processes input while the dialog blocks its thread.
            screen.query_one("#wizard-custom-root", Input).value = typed
            await pilot.pause()
            assert screen.state.custom_root == typed

            picker.release()
            await settle(pilot)
            assert not screen.picker_busy
            assert str(browse.label) == BROWSE_LABEL
            assert screen.state.custom_root == typed, "cancel keeps what is there"

    try:
        run(exercise)
    finally:
        picker.release()


def test_a_picker_that_raises_is_a_cancel_not_a_crash(tmp_path):
    class Exploding(FakeFolderPicker):
        def choose_folder(self, initial_path=None):
            super().choose_folder(initial_path)
            raise OSError("COM exploded")

    picker = Exploding()

    async def exercise() -> None:
        app, _ = hub_with(picker=picker)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            await click(pilot, "#wizard-custom-browse")
            await settle(pilot)
            assert not screen.picker_busy
            assert screen.state.custom_root == ""

    run(exercise)


def test_the_picked_folder_goes_through_the_same_validation_as_typing():
    picker = FakeFolderPicker(result=Path("relative") / "folder")

    async def exercise() -> None:
        app, _ = hub_with(picker=picker)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            await click(pilot, "#wizard-custom-browse")
            await settle(pilot)
            assert "Caminho inválido" in text(app, "#wizard-custom-error")
            assert screen.state.selection is None
            assert screen.query_one("#wizard-next", Button).disabled

    run(exercise)


def test_manual_typing_still_works_with_a_picker_present(tmp_path):
    typed = str(tmp_path / "manual")

    async def exercise() -> None:
        app, _ = hub_with(picker=FakeFolderPicker())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            field = screen.query_one("#wizard-custom-root", Input)
            field.focus()
            await pilot.press(*typed)
            await pilot.pause()
            assert screen.state.custom_root == typed
            assert screen.state.custom_error == ""

    run(exercise)


def test_without_a_picker_the_button_is_not_offered():
    async def exercise() -> None:
        app, _ = hub_with(picker=None)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            assert not screen.query_one("#wizard-custom-browse", Button).display
            assert screen.query_one("#wizard-custom-root", Input).display

    run(exercise)


def test_the_picker_is_only_offered_for_a_custom_location():
    async def exercise() -> None:
        app, _ = hub_with(picker=FakeFolderPicker())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            for mode in ("default", "portable"):
                await click(pilot, f"#wizard-mode-{mode}")
                assert not screen.query_one("#wizard-custom-browse", Button).display
                assert not screen.query_one("#wizard-recent-roots", Select).display
                assert not screen.query_one("#wizard-recent-empty", Static).display

    run(exercise)


# --- recents in the wizard --------------------------------------------------------------------


def test_no_history_shows_a_discreet_empty_state():
    async def exercise() -> None:
        app, _ = hub_with(FakeHubApplication(), picker=FakeFolderPicker())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            assert not screen.query_one("#wizard-recent-roots", Select).display
            empty = screen.query_one("#wizard-recent-empty", Static)
            assert empty.display
            assert str(empty.content) == NO_RECENT_TEXT

    run(exercise)


@pytest.mark.parametrize("count", [1, 5])
def test_history_is_offered_and_selecting_one_fills_the_field(tmp_path, count):
    roots = tuple(str(tmp_path / f"r{index}") for index in range(count))

    async def exercise() -> None:
        app, fake = hub_with(FakeHubApplication(recent_roots=roots), picker=FakeFolderPicker())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            select = screen.query_one("#wizard-recent-roots", Select)
            assert select.display
            assert not screen.query_one("#wizard-recent-empty", Static).display
            assert screen.recent_roots == roots

            select.value = roots[-1]
            await pilot.pause()
            await pilot.pause()
            assert screen.query_one("#wizard-custom-root", Input).value == roots[-1]
            assert screen.state.custom_root == roots[-1]
            assert screen.state.custom_error == ""
            assert screen.state.step is WizardStep.MODE
            assert fake.calls == [], "a recent path never starts an install"

    run(exercise)


def test_a_recent_folder_that_no_longer_exists_does_not_crash(tmp_path):
    gone = str(tmp_path / "removed-since")

    async def exercise() -> None:
        app, _ = hub_with(FakeHubApplication(recent_roots=(gone,)), picker=FakeFolderPicker())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            assert not Path(gone).exists()
            screen.query_one("#wizard-recent-roots", Select).value = gone
            await pilot.pause()
            await pilot.pause()
            # Still a valid destination: the same rule as typing it.
            assert screen.state.custom_root == gone
            assert screen.state.custom_error == ""
            assert screen.recent_roots == (gone,), "not silently removed"

    run(exercise)


def test_a_failed_attempt_is_offered_again_on_retry(tmp_path):
    root = str(tmp_path / "Capellah_gpt")
    fake = FakeHubApplication(install_fail_at="Instalando ComfyUI")

    async def exercise() -> None:
        app, _ = hub_with(fake, picker=FakeFolderPicker())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            screen.fill_custom_root(root)
            await pilot.pause()
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)
            assert screen.state.step is WizardStep.DONE
            assert screen.operation is not None and screen.operation.failed

            assert fake.recent_roots == (root,)
            assert screen.recent_roots == (root,)
            await click(pilot, "#wizard-retry")
            await click(pilot, "#wizard-back")
            assert screen.state.step is WizardStep.MODE
            select = screen.query_one("#wizard-recent-roots", Select)
            assert select.display
            select.value = root  # raises if the option were missing
            await pilot.pause()

    run(exercise)


def test_default_and_portable_attempts_do_not_enter_the_wizard_history():
    fake = FakeHubApplication()

    async def exercise() -> None:
        app, _ = hub_with(fake, picker=FakeFolderPicker())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            screen = wizard(app)
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-profile-capella")
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-mode-portable")
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)
            assert fake.calls and fake.calls[0][1].mode is InstallLocationMode.PORTABLE
            assert fake.recent_roots == ()
            assert screen.recent_roots == ()

    run(exercise)


# --- keyboard, text, layout ------------------------------------------------------------------


def test_the_custom_row_is_usable_from_the_keyboard(tmp_path):
    chosen = tmp_path / "by-keyboard"
    recent = str(tmp_path / "recent-by-keyboard")
    picker = FakeFolderPicker(result=chosen)

    async def exercise() -> None:
        app, _ = hub_with(FakeHubApplication(recent_roots=(recent,)), picker=picker)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)

            screen.query_one("#wizard-custom-root", Input).focus()
            await pilot.pause()
            await pilot.press("tab")
            assert app.focused is screen.query_one("#wizard-custom-browse", Button)
            await pilot.press("enter")
            await settle(pilot)
            assert screen.state.custom_root == str(chosen)
            assert app.focused is screen.query_one("#wizard-custom-browse", Button), (
                "the keyboard user keeps their place after the dialog closes"
            )

            await pilot.press("tab")
            select = screen.query_one("#wizard-recent-roots", Select)
            assert app.focused is select
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert screen.state.custom_root == recent

            await click(pilot, "#wizard-next")
            assert screen.state.step is WizardStep.REVIEW

    run(exercise)


def test_zero_emoji_in_the_custom_folder_step():
    async def exercise() -> None:
        app, _ = hub_with(
            FakeHubApplication(recent_roots=(r"C:\AI\Pepito",)), picker=FakeFolderPicker()
        )
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            shown = " ".join(
                [
                    str(screen.query_one("#wizard-custom-label", Static).content),
                    str(screen.query_one("#wizard-recent-label", Static).content),
                    str(screen.query_one("#wizard-recent-empty", Static).content),
                    str(screen.query_one("#wizard-custom-browse", Button).label),
                    BROWSE_BUSY_LABEL,
                    text(app, "#wizard-mode-summary"),
                ]
            )
            assert unsafe_characters(shown) == ()

    run(exercise)


@pytest.mark.parametrize("width", [120, 100, 80, 70])
def test_the_custom_folder_row_fits_at_several_widths(width):
    async def exercise() -> None:
        app, _ = hub_with(
            FakeHubApplication(recent_roots=(r"C:\Users\someone\Desktop\Capellah_gpt",)),
            picker=FakeFolderPicker(),
        )
        async with app.run_test(size=(width, 50)) as pilot:
            await pilot.pause()
            screen = await to_custom(pilot)
            for selector in (
                "#wizard-custom-root",
                "#wizard-custom-browse",
                "#wizard-recent-roots",
            ):
                widget = screen.query_one(selector)
                assert widget.display, selector
                assert widget.size.width > 0, selector
                assert widget.region.right <= width, (selector, widget.region, width)

    run(exercise)
