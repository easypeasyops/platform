from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.main import app

client = TestClient(app)


def _count(route: str, status: str) -> float:
    labels = {"method": "GET", "route": route, "status": status}
    return REGISTRY.get_sample_value("http_requests_total", labels) or 0.0


def test_the_front_page_is_the_monitors_tab_for_now() -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/monitors"


def test_the_probes_answer() -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_requests_are_counted_by_route_and_status() -> None:
    before = _count("/healthz", "200")
    client.get("/healthz")
    assert _count("/healthz", "200") == before + 1
    labels = {"method": "GET", "route": "/healthz"}
    assert REGISTRY.get_sample_value("http_request_duration_seconds_count", labels)


def test_unknown_paths_share_one_route_label() -> None:
    before = _count("unmatched", "404")
    assert client.get("/no/such/path").status_code == 404
    assert client.get("/another").status_code == 404
    assert _count("unmatched", "404") == before + 2
