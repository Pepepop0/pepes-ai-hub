from local_ai_stack.health import check_port


def test_closed_high_port_does_not_crash():
    r = check_port("fixture", "127.0.0.1", 65534, timeout=0.01)
    assert r.name == "fixture" and isinstance(r.reachable, bool)
