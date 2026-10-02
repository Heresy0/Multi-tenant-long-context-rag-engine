from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.frontend import router


@pytest.fixture
def client():
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        oidc_issuer="http://127.0.0.1:8080/realms/enterprise-knowledge",
        oidc_frontend_client_id="enterprise-knowledge-web",
        max_upload_bytes=20971520,
        database_url="must-not-be-exposed",
        chat_api_key="must-not-be-exposed",
    )
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


def test_frontend_page_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "知序" in response.text
    assert "/ui/assets/app.js" in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "connect-src 'self' http://127.0.0.1:8080" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


def test_config_only_exposes_public_browser_settings(client):
    response = client.get("/ui/config")
    assert response.json() == {
        "issuer": "http://127.0.0.1:8080/realms/enterprise-knowledge",
        "client_id": "enterprise-knowledge-web",
        "max_upload_bytes": 20971520,
    }
    assert "must-not-be-exposed" not in response.text


@pytest.mark.parametrize("asset", ["app.js", "auth.js", "styles.css"])
def test_browser_assets_are_served(client, asset):
    response = client.get(f"/ui/assets/{asset}")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert len(response.content) > 100


@pytest.mark.parametrize("path", ["/ui/assets/.env", "/ui/assets/config.py", "/ui/assets/%2e%2e%2fconfig.py"])
def test_assets_cannot_read_arbitrary_files(client, path):
    assert client.get(path).status_code == 404


def test_invalid_issuer_does_not_enter_security_header(client):
    client.app.state.settings.oidc_issuer = "http://bad';script-src/realm"
    assert client.get("/").status_code == 503
    assert client.get("/ui/config").status_code == 503
