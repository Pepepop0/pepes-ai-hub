from local_ai_stack.health import DEFAULT_SERVICES, check_default_services, check_port
from local_ai_stack.services import DEFAULT_SERVICE_SPECS


def test_closed_high_port_does_not_crash():
    r = check_port("fixture", "127.0.0.1", 65534, timeout=0.01)
    assert r.name == "fixture" and isinstance(r.reachable, bool)


def test_legacy_defaults_track_the_service_specs():
    assert DEFAULT_SERVICES == tuple((s.name, s.host, s.port) for s in DEFAULT_SERVICE_SPECS)


def test_check_default_services_returns_one_entry_per_service():
    results = check_default_services()
    assert [r.name for r in results] == [s.name for s in DEFAULT_SERVICE_SPECS]
    assert all(isinstance(r.reachable, bool) for r in results)
