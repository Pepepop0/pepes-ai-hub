from __future__ import annotations

from local_ai_stack.layout import StackLayout
from local_ai_stack.logs import LogFiles, read_log_tail, service_log_files


def test_service_log_files_come_from_the_stack_layout(tmp_path):
    layout = StackLayout.from_root(tmp_path / "root")
    logs = service_log_files(layout, "ollama")
    assert logs.stdout == layout.log_file("ollama")
    assert logs.stderr == layout.log_file("ollama-stderr")
    assert not logs.merged
    assert logs.paths == (logs.stdout, logs.stderr)


def test_stderr_can_be_merged_into_stdout(tmp_path):
    layout = StackLayout.from_root(tmp_path / "root")
    logs = service_log_files(layout, "ollama", split_stderr=False)
    assert logs.merged
    assert logs.paths == (logs.stdout,)


def test_ensure_parents_creates_only_directories(tmp_path):
    logs = LogFiles(tmp_path / "logs" / "a.log", tmp_path / "other" / "b.log")
    logs.ensure_parents()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "other").is_dir()
    assert not logs.stdout.exists()


def test_tail_returns_the_last_lines_only(tmp_path):
    path = tmp_path / "big.log"
    path.write_text("\n".join(f"line {i}" for i in range(5000)) + "\n", encoding="utf-8")
    tail = read_log_tail(path, 3)
    assert tail == ("line 4997", "line 4998", "line 4999")


def test_tail_handles_a_file_shorter_than_the_request(tmp_path):
    path = tmp_path / "small.log"
    path.write_text("only\n", encoding="utf-8")
    assert read_log_tail(path, 50) == ("only",)


def test_tail_normalises_windows_line_endings(tmp_path):
    path = tmp_path / "crlf.log"
    path.write_bytes(b"a\r\nb\r\n")
    assert read_log_tail(path) == ("a", "b")


def test_tail_survives_undecodable_bytes(tmp_path):
    path = tmp_path / "binary.log"
    path.write_bytes(b"ok\n\xff\xfe\n")
    assert read_log_tail(path)[0] == "ok"


def test_tail_of_a_missing_file_or_zero_lines_is_empty(tmp_path):
    path = tmp_path / "nope.log"
    assert read_log_tail(path) == ()
    path.write_text("x\n", encoding="utf-8")
    assert read_log_tail(path, 0) == ()
