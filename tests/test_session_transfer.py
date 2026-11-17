import pytest

from local_ai_stack.session_transfer import fetch_session


@pytest.mark.parametrize(
    "host", ["-oProxyCommand=anything", "root@host;touch pwned", "host\ncommand", "host name"]
)
def test_invalid_ssh_host_is_rejected_before_creating_output(tmp_path, host):
    destination = tmp_path / "new" / "session.zip"
    with pytest.raises(ValueError):
        fetch_session(
            host=host,
            port=22,
            identity_file=None,
            bundle="/opt/bench",
            root="/mnt/capella",
            destination=destination,
        )
    assert not destination.parent.exists()


@pytest.mark.parametrize(
    "root", ["/mnt/a;touch-pwned", "/mnt/a$(id)", "/mnt/a`id`", "/mnt/a b", "/mnt/../etc", "/"]
)
def test_legacy_scp_metacharacters_and_traversal_cannot_reach_a_connection(tmp_path, root):
    destination = tmp_path / "new" / "session.zip"
    with pytest.raises(ValueError):
        fetch_session(
            host="root@example.invalid",
            port=22,
            identity_file=None,
            bundle="/opt/bench",
            root=root,
            destination=destination,
        )
    assert not destination.parent.exists()


def test_fetch_never_overwrites_existing_local_evidence(tmp_path):
    destination = tmp_path / "session.zip"
    destination.write_bytes(b"existing evidence")
    with pytest.raises(ValueError):
        fetch_session(
            host="root@example.invalid",
            port=22,
            identity_file=None,
            bundle="/opt/bench",
            root="/mnt/capella",
            destination=destination,
        )
    assert destination.read_bytes() == b"existing evidence"
