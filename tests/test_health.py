from fastapi.testclient import TestClient

from wardlock.main import app


def test_health_returns_ok():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_root_url_is_a_landing_page_not_a_404():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Wardlock" in response.text and "/mcp" in response.text

