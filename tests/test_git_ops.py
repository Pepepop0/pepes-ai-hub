"""Tests for the Git layer.

No real repository is cloned, fetched or checked out: the CommandRunner is
replaced by a fake that records argv and returns canned results.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from local_ai_stack.git_ops import (
    DirtyWorkingTreeError,
    GitClient,
    GitCommandError,
    GitError,
    GitNotAvailableError,
    clone_argv,
)
from local_ai_stack.runner import CommandResult, CommandRunner


class FakeRunner(CommandRunner):
    """Records argv, answers from a table keyed by the git subcommand."""

    def __init__(self, outputs: Mapping[str, CommandResult] | None = None, *, git: bool = True):
        super().__init__()
        self.outputs = dict(outputs or {})
        self.git_available = git
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, object]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return "C:\\git.exe" if (name == "git" and self.git_available) else None

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)
        self.kwargs.append(dict(kwargs))
        if not self.git_available:
            return CommandResult(argv=items, returncode=127, not_found=True, stderr="not found")
        result = self.outputs.get(self._key(items))
        return result if result is not None else ok("")

    @staticmethod
    def _key(argv: Sequence[str]) -> str:
        if len(argv) < 2:
            return ""
        # rev-parse serve a duas perguntas distintas; a chave precisa distingui-las.
        if argv[1] == "rev-parse" and len(argv) > 2:
            return f"rev-parse {argv[2]}"
        return argv[1]

    def argv_for(self, subcommand: str) -> tuple[str, ...] | None:
        for call in self.calls:
            if len(call) > 1 and call[1] == subcommand:
                return call
        return None


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=("git",), returncode=0, stdout=stdout)


def fail(stderr: str = "boom", returncode: int = 1) -> CommandResult:
    return CommandResult(argv=("git",), returncode=returncode, stderr=stderr)


IS_REPO = "rev-parse --is-inside-work-tree"
HEAD = "rev-parse HEAD"
CLEAN = {"status": ok(""), IS_REPO: ok("true\n")}
HEAD_SHA = "1234567890abcdef1234567890abcdef12345678"


def client_with(
    extra: Mapping[str, CommandResult] | None = None, **outputs: CommandResult
) -> tuple[GitClient, FakeRunner]:
    runner = FakeRunner({**CLEAN, **(extra or {}), **outputs})
    return GitClient(runner), runner


# --- disponibilidade -------------------------------------------------------


def test_git_detected():
    client, _ = client_with()
    assert client.available() is True
    client.require_available()


def test_git_absent_is_reported_not_crashed():
    client = GitClient(FakeRunner(git=False))
    assert client.available() is False
    with pytest.raises(GitNotAvailableError):
        client.require_available()


def test_git_absent_raises_on_any_command(tmp_path):
    client = GitClient(FakeRunner(git=False))
    with pytest.raises(GitNotAvailableError):
        client.head(tmp_path)


def test_version_is_parsed():
    client, _ = client_with(**{"--version": ok("git version 2.50.0.windows.2\n")})
    assert client.version() == "2.50.0.windows.2"


def test_unexpected_version_output_is_none():
    client, _ = client_with(**{"--version": ok("something else\n")})
    assert client.version() is None


# --- clone -----------------------------------------------------------------


def test_clone_argv_is_structured(tmp_path):
    argv = clone_argv("https://example.invalid/repo.git", tmp_path / "dest")
    assert argv[:2] == ["git", "clone"]
    assert "--" in argv
    assert argv[-2] == "https://example.invalid/repo.git"
    assert argv[-1] == str(tmp_path / "dest")


def test_clone_uses_a_separator_before_the_url(tmp_path):
    client, runner = client_with()
    client.clone("https://example.invalid/r.git", tmp_path / "dest")
    call = runner.argv_for("clone")
    assert call is not None
    assert call[0] == "git"
    assert "--" in call
    assert call.index("--") < call.index("https://example.invalid/r.git")


def test_clone_never_uses_a_shell(tmp_path):
    client, runner = client_with()
    client.clone("https://example.invalid/r.git", tmp_path / "dest")
    assert all(isinstance(part, str) for part in runner.calls[0])
    assert not any("&&" in part for part in runner.calls[0])


def test_clone_refuses_a_non_empty_destination(tmp_path):
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "file.txt").write_text("x", encoding="utf-8")
    client, runner = client_with()
    with pytest.raises(GitError) as exc:
        client.clone("https://example.invalid/r.git", destination)
    assert "not empty" in str(exc.value)
    assert runner.calls == []


def test_clone_refuses_a_file_destination(tmp_path):
    destination = tmp_path / "dest"
    destination.write_text("i am a file", encoding="utf-8")
    client, _ = client_with()
    with pytest.raises(GitError):
        client.clone("https://example.invalid/r.git", destination)


def test_clone_accepts_an_empty_existing_directory(tmp_path):
    destination = tmp_path / "dest"
    destination.mkdir()
    client, runner = client_with()
    client.clone("https://example.invalid/r.git", destination)
    assert runner.argv_for("clone") is not None


def test_clone_with_revision_pins_by_checkout(tmp_path):
    client, runner = client_with()
    client.clone("https://example.invalid/r.git", tmp_path / "dest", revision="v1.2.3")
    checkout = runner.argv_for("checkout")
    assert checkout is not None
    assert "v1.2.3" in checkout
    assert "--detach" in checkout


def test_shallow_clone_cannot_be_pinned_by_revision(tmp_path):
    client, _ = client_with()
    with pytest.raises(GitError):
        client.clone("https://e.invalid/r.git", tmp_path / "d", revision="abc", depth=1)


def test_clone_failure_raises_a_command_error(tmp_path):
    client, _ = client_with(clone=fail("repository not found"))
    with pytest.raises(GitCommandError) as exc:
        client.clone("https://example.invalid/r.git", tmp_path / "dest")
    assert exc.value.result is not None


# --- repositorio existente -------------------------------------------------


def test_existing_repository_is_recognised(tmp_path):
    client, _ = client_with({IS_REPO: ok("true\n")})
    assert client.is_repository(tmp_path) is True


def test_non_repository_directory(tmp_path):
    client, _ = client_with({IS_REPO: fail("not a git repository")})
    assert client.is_repository(tmp_path) is False


def test_missing_directory_is_not_a_repository(tmp_path):
    client, runner = client_with()
    assert client.is_repository(tmp_path / "absent") is False
    assert runner.calls == []


def test_head_returns_the_commit(tmp_path):
    client, _ = client_with({HEAD: ok(f"{HEAD_SHA}\n")})
    assert client.head(tmp_path) == HEAD_SHA


def test_head_failure_is_typed(tmp_path):
    client, _ = client_with({HEAD: fail("bad object")})
    with pytest.raises(GitCommandError):
        client.head(tmp_path)


def test_remote_url_is_read(tmp_path):
    client, _ = client_with(remote=ok("https://example.invalid/r.git\n"))
    assert client.remote_url(tmp_path) == "https://example.invalid/r.git"


def test_missing_remote_is_none(tmp_path):
    client, _ = client_with(remote=fail("no such remote"))
    assert client.remote_url(tmp_path) is None


def test_status_snapshot_of_a_non_repository(tmp_path):
    client, _ = client_with({IS_REPO: fail("nope")})
    status = client.status(tmp_path)
    assert status.is_repository is False
    assert status.head is None and status.dirty is None


def test_status_snapshot_of_a_repository(tmp_path):
    runner = FakeRunner(
        {
            IS_REPO: ok("true\n"),
            HEAD: ok(f"{HEAD_SHA}\n"),
            "status": ok(""),
            "remote": ok("https://e.invalid/r\n"),
        }
    )
    status = GitClient(runner).status(tmp_path)
    assert status.is_repository is True
    assert status.head == HEAD_SHA
    assert status.dirty is False
    assert status.remote_url == "https://e.invalid/r"


# --- clean vs dirty --------------------------------------------------------


def test_clean_working_tree(tmp_path):
    client, _ = client_with(status=ok(""))
    assert client.is_dirty(tmp_path) is False


def test_dirty_working_tree(tmp_path):
    client, _ = client_with(status=ok(" M src/file.py\n"))
    assert client.is_dirty(tmp_path) is True


def test_checkout_refuses_a_dirty_tree(tmp_path):
    client, runner = client_with(status=ok(" M src/file.py\n"))
    with pytest.raises(DirtyWorkingTreeError):
        client.checkout(tmp_path, "v1.0.0")
    assert runner.argv_for("checkout") is None


def test_fetch_refuses_a_dirty_tree(tmp_path):
    client, runner = client_with(status=ok("?? junk\n"))
    with pytest.raises(DirtyWorkingTreeError):
        client.fetch(tmp_path)
    assert runner.argv_for("fetch") is None


def test_dirty_tree_can_be_overridden_explicitly(tmp_path):
    client, runner = client_with(status=ok(" M f\n"))
    client.checkout(tmp_path, "v1.0.0", allow_dirty=True)
    assert runner.argv_for("checkout") is not None


# --- pinning ---------------------------------------------------------------


def test_checkout_pins_a_detached_revision(tmp_path):
    client, runner = client_with()
    client.checkout(tmp_path, HEAD_SHA)
    assert runner.argv_for("checkout") == ("git", "checkout", "--detach", HEAD_SHA)


def test_empty_revision_is_rejected(tmp_path):
    client, _ = client_with()
    with pytest.raises(GitError):
        client.checkout(tmp_path, "   ")


def test_fetch_never_merges_or_resets(tmp_path):
    client, runner = client_with()
    client.fetch(tmp_path)
    call = runner.argv_for("fetch")
    assert call is not None
    assert "--tags" in call and "--prune" in call


def test_no_command_ever_resets_or_touches_global_config(tmp_path):
    client, runner = client_with()
    client.clone("https://e.invalid/r.git", tmp_path / "d", revision="v1")
    client.fetch(tmp_path)
    client.status(tmp_path)
    # Inspeciona tokens, nao a linha inteira: o proprio tmp_path pode conter
    # a palavra "reset" e produzir um falso positivo.
    subcommands = {call[1] for call in runner.calls if len(call) > 1}
    assert "reset" not in subcommands
    assert "config" not in subcommands
    flags = {token for call in runner.calls for token in call if token.startswith("--")}
    assert "--global" not in flags
    assert "--hard" not in flags


# --- dry-run e timeout -----------------------------------------------------


def test_dry_run_client_does_not_mutate(tmp_path):
    runner = FakeRunner(CLEAN)
    client = GitClient(runner, dry_run=True)
    client.clone("https://example.invalid/r.git", tmp_path / "dest")
    clone_call_index = next(i for i, c in enumerate(runner.calls) if c[1] == "clone")
    assert runner.kwargs[clone_call_index]["dry_run"] is True


def test_read_only_queries_always_really_run(tmp_path):
    runner = FakeRunner({**CLEAN, HEAD: ok(f"{HEAD_SHA}\n")})
    client = GitClient(runner, dry_run=True)
    assert client.head(tmp_path) == HEAD_SHA
    assert all(kw.get("dry_run") is False for kw in runner.kwargs)


def test_timeout_is_passed_to_every_command(tmp_path):
    runner = FakeRunner(CLEAN)
    GitClient(runner, timeout=42.0).fetch(tmp_path)
    assert all(kw.get("timeout") == 42.0 for kw in runner.kwargs)


def test_paths_with_spaces_and_apostrophes(tmp_path):
    destination = tmp_path / "IA's" / "repo dir"
    client, runner = client_with()
    client.clone("https://example.invalid/r.git", destination)
    call = runner.argv_for("clone")
    assert call is not None
    assert call[-1] == str(destination)


def test_real_repository_is_readable_without_mutation():
    """Smoke: this worktree is a repository and stays untouched."""
    repo = Path(__file__).resolve().parents[1]
    client = GitClient()
    if not client.available():
        pytest.skip("git is not installed")
    status = client.status(repo)
    assert status.is_repository is True
    assert status.head is not None and len(status.head) == 40
