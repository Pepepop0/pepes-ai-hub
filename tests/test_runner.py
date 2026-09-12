"""Testes do CommandRunner.

Nada aqui instala software, baixa arquivo ou altera PATH global.
Todo comando externo e o proprio interpretador Python (`sys.executable`).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

from local_ai_stack.runner import (
    RETURNCODE_NOT_FOUND,
    RETURNCODE_TIMEOUT,
    CommandFailedError,
    CommandNotFoundError,
    CommandRunner,
    CommandTimeoutError,
    sanitize,
    sanitize_argv,
)

PY = sys.executable


def py(code: str) -> list[str]:
    return [PY, "-c", code]


# --- sucesso ---------------------------------------------------------------


def test_success_captures_stdout_and_exit_code():
    r = CommandRunner().run(py("print('hello')"))
    assert r.ok
    assert r.returncode == 0
    assert "hello" in r.stdout
    assert r.stderr == ""
    assert not r.dry_run and not r.timed_out and not r.not_found
    assert r.duration_s >= 0.0


def test_success_with_check_does_not_raise():
    r = CommandRunner().run(py("print('ok')"), check=True)
    assert r.ok


def test_stdin_is_forwarded():
    r = CommandRunner().run(
        py("import sys; print(sys.stdin.read().strip().upper())"), input_text="abc"
    )
    assert "ABC" in r.stdout


def test_cwd_is_respected(tmp_path):
    r = CommandRunner().run(py("import os; print(os.getcwd())"), cwd=tmp_path)
    assert str(tmp_path).lower() in r.stdout.strip().lower()


# --- erro ------------------------------------------------------------------


def test_nonzero_exit_is_preserved_and_does_not_raise():
    r = CommandRunner().run(py("import sys; sys.stderr.write('boom'); sys.exit(3)"))
    assert r.returncode == 3
    assert not r.ok
    assert "boom" in r.stderr


def test_check_raises_on_nonzero_exit():
    with pytest.raises(CommandFailedError) as exc:
        CommandRunner().run(py("import sys; sys.exit(7)"), check=True)
    assert exc.value.result is not None
    assert exc.value.result.returncode == 7


def test_missing_executable_is_predictable():
    r = CommandRunner().run(["local-ai-stack-nao-existe-xyz"])
    assert r.not_found
    assert r.returncode == RETURNCODE_NOT_FOUND
    assert not r.ok
    assert "not found" in r.stderr


def test_missing_executable_with_check_raises():
    with pytest.raises(CommandNotFoundError):
        CommandRunner().run(["local-ai-stack-nao-existe-xyz"], check=True)


# --- timeout ---------------------------------------------------------------


def test_timeout_is_flagged_and_does_not_raise():
    r = CommandRunner().run(py("import time; time.sleep(5)"), timeout=0.3)
    assert r.timed_out
    assert r.returncode == RETURNCODE_TIMEOUT
    assert not r.ok


def test_timeout_with_check_raises():
    with pytest.raises(CommandTimeoutError) as exc:
        CommandRunner().run(py("import time; time.sleep(5)"), timeout=0.3, check=True)
    assert exc.value.result is not None
    assert exc.value.result.timed_out


def test_default_timeout_is_used_when_not_given():
    runner = CommandRunner(default_timeout=0.3)
    r = runner.run(py("import time; time.sleep(5)"))
    assert r.timed_out


def test_invalid_timeout_rejected():
    with pytest.raises(ValueError):
        CommandRunner().run(py("pass"), timeout=0)
    with pytest.raises(ValueError):
        CommandRunner(default_timeout=-1)


# --- dry-run ---------------------------------------------------------------


def test_dry_run_does_not_execute(tmp_path):
    marker = tmp_path / "criado.txt"
    code = f"open({str(marker)!r}, 'w').close()"
    r = CommandRunner(dry_run=True).run(py(code))
    assert r.dry_run
    assert r.returncode == 0
    assert r.ok
    assert r.stdout == "" and r.stderr == ""
    assert not marker.exists()


def test_dry_run_per_call_override(tmp_path):
    marker = tmp_path / "criado2.txt"
    code = f"open({str(marker)!r}, 'w').close()"
    runner = CommandRunner()
    assert runner.run(py(code), dry_run=True).dry_run
    assert not marker.exists()
    assert runner.run(py(code)).ok
    assert marker.exists()


def test_dry_run_never_raises_for_missing_executable():
    r = CommandRunner(dry_run=True).run(["local-ai-stack-nao-existe-xyz"], check=True)
    assert r.ok and r.dry_run


def test_dry_run_still_validates_argv():
    with pytest.raises(TypeError):
        CommandRunner(dry_run=True).run("echo hi")


# --- contrato: sem shell ---------------------------------------------------


def test_string_argv_is_rejected():
    with pytest.raises(TypeError):
        CommandRunner().run("echo hi")


def test_empty_argv_is_rejected():
    with pytest.raises(ValueError):
        CommandRunner().run([])


def test_non_string_argv_item_is_rejected():
    with pytest.raises(TypeError):
        CommandRunner().run([PY, 42])


def test_pathlike_argv_item_is_accepted(tmp_path):
    script = tmp_path / "s.py"
    script.write_text("print('via path')", encoding="utf-8")
    r = CommandRunner().run([PY, script])
    assert "via path" in r.stdout


def test_shell_metacharacters_are_not_interpreted():
    r = CommandRunner().run(py("import sys; print(sys.argv[1])") + ["a && b > c"])
    assert "a && b > c" in r.stdout


def test_run_never_uses_shell(monkeypatch):
    seen: dict[str, object] = {}
    real = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    CommandRunner().run(py("pass"))
    assert seen.get("shell") is False


# --- ambiente / PATH -------------------------------------------------------


def test_env_extra_reaches_child_without_touching_os_environ():
    before = dict(os.environ)
    r = CommandRunner().run(
        py("import os; print(os.environ.get('LAS_TEST_VAR', 'MISSING'))"),
        env={"LAS_TEST_VAR": "42"},
    )
    assert "42" in r.stdout
    assert "LAS_TEST_VAR" not in os.environ
    assert dict(os.environ) == before


def test_env_overrides_do_not_mutate_global_path():
    path_before = os.environ.get("PATH")
    runner = CommandRunner(env={"PATH": "C:\\fake"})
    assert runner.child_env()["PATH"] == "C:\\fake"
    assert os.environ.get("PATH") == path_before


def test_which_uses_child_path():
    runner = CommandRunner(env={"PATH": ""})
    assert runner.which("git") is None
    assert runner.available("git") is False


# --- sanitizacao -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, must_not_contain",
    [
        ("https://pedro:s3cr3t@github.com/x.git", "s3cr3t"),
        ("https://host/f?token=abcdef123456", "abcdef123456"),
        ("https://host/f?api_key=abcdef123456&x=1", "abcdef123456"),
        ("--token=abcdef123456", "abcdef123456"),
        ("--api-key=abcdef123456", "abcdef123456"),
        ("Authorization: abcdef123456", "abcdef123456"),
        ("Bearer abcdef123456", "abcdef123456"),
        ("hf_abcdefghijklmnopqrstuvwxyz", "hf_abcdefghijklmnopqrstuvwxyz"),
        ("ghp_abcdefghijklmnopqrstuvwxyz", "ghp_abcdefghijklmnopqrstuvwxyz"),
        ("sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnopqrstuvwxyz"),
    ],
)
def test_sanitize_removes_secrets(raw, must_not_contain):
    out = sanitize(raw)
    assert must_not_contain not in out
    assert "***" in out


def test_sanitize_keeps_harmless_text():
    assert sanitize("git clone https://github.com/user/repo.git") == (
        "git clone https://github.com/user/repo.git"
    )
    assert sanitize("") == ""


def test_sanitize_argv_redacts_value_after_secret_flag():
    out = sanitize_argv(["curl", "--token", "abcdef123456", "https://host"])
    assert "abcdef123456" not in out
    assert out[2] == "***"


def test_result_display_is_sanitized():
    r = CommandRunner(dry_run=True).run([PY, "--token", "abcdef123456"])
    assert "abcdef123456" not in r.display
    assert "abcdef123456" not in str(r)
    assert r.argv[2] == "abcdef123456"


def test_logs_are_sanitized(caplog):
    caplog.set_level(logging.DEBUG, logger="local_ai_stack.runner")
    CommandRunner(dry_run=True).run([PY, "--password=hunter2-super-secret"])
    assert "hunter2-super-secret" not in caplog.text
    assert "***" in caplog.text


def test_failed_command_stderr_is_sanitized_in_exception():
    code = "import sys; sys.stderr.write('Bearer abcdef123456'); sys.exit(1)"
    with pytest.raises(CommandFailedError) as exc:
        CommandRunner().run(py(code), check=True)
    assert "abcdef123456" not in str(exc.value)


# --- on_output: saida ao vivo -----------------------------------------------


def test_on_output_receives_each_line_as_the_process_writes_it():
    seen: list[tuple[str, str]] = []
    code = "print('one'); print('two'); import sys; sys.stderr.write('err\\n')"
    r = CommandRunner().run(py(code), on_output=lambda stream, line: seen.append((stream, line)))
    assert r.ok
    assert ("stdout", "one") in seen
    assert ("stdout", "two") in seen
    assert ("stderr", "err") in seen


def test_on_output_result_matches_the_non_streaming_result():
    code = "print('hello')"
    streamed = CommandRunner().run(py(code), on_output=lambda *_: None)
    plain = CommandRunner().run(py(code))
    assert streamed.returncode == plain.returncode
    assert streamed.stdout == plain.stdout
    assert streamed.ok


def test_on_output_sees_a_carriage_return_updated_line_as_its_own_line():
    """A pip-style progress bar redraws with `\\r`, not `\\n`."""
    code = (
        "import sys, time\n"
        "for i in range(3):\n"
        "    sys.stdout.write(f'\\r{i}/2 done')\n"
        "    sys.stdout.flush()\n"
        "sys.stdout.write('\\n')\n"
    )
    seen: list[str] = []
    CommandRunner().run(py(code), on_output=lambda _stream, line: seen.append(line))
    assert "0/2 done" in seen
    assert "1/2 done" in seen
    assert "2/2 done" in seen


def test_on_output_preserves_nonzero_exit_code():
    r = CommandRunner().run(
        py("import sys; print('before'); sys.exit(5)"), on_output=lambda *_: None
    )
    assert r.returncode == 5
    assert not r.ok


def test_on_output_preserves_timeout_behavior():
    r = CommandRunner().run(
        py("import time; time.sleep(5)"), timeout=0.3, on_output=lambda *_: None
    )
    assert r.timed_out
    assert r.returncode == RETURNCODE_TIMEOUT


def test_on_output_preserves_missing_executable_behavior():
    r = CommandRunner().run(["local-ai-stack-nao-existe-xyz"], on_output=lambda *_: None)
    assert r.not_found
    assert r.returncode == RETURNCODE_NOT_FOUND


def test_on_output_check_still_raises_on_failure():
    with pytest.raises(CommandFailedError):
        CommandRunner().run(py("import sys; sys.exit(3)"), on_output=lambda *_: None, check=True)


def test_on_output_is_rejected_together_with_input_text():
    with pytest.raises(ValueError):
        CommandRunner().run(py("pass"), on_output=lambda *_: None, input_text="x")


def test_dry_run_never_invokes_on_output():
    seen = []
    r = CommandRunner(dry_run=True).run(py("print('nope')"), on_output=seen.append)
    assert r.dry_run
    assert seen == []


def test_a_broken_on_output_callback_does_not_lose_the_result():
    def explode(_stream: str, _line: str) -> None:
        raise RuntimeError("bad parser")

    r = CommandRunner().run(py("print('one'); print('two')"), on_output=explode)
    assert r.ok
    assert "one" in r.stdout and "two" in r.stdout
