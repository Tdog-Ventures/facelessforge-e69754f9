"""Exercise the asset-path rejection through FastAPI request validation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.routes import api


@pytest.fixture
def http_boundary(monkeypatch):
    insert = AsyncMock()
    database = SimpleNamespace(
        projects=SimpleNamespace(find_one=AsyncMock(return_value={"id": "project", "user_id": "owner"})),
        assets=SimpleNamespace(insert_one=insert),
    )
    monkeypatch.setattr(api, "get_db", lambda: database)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: {"id": "owner", "role": "creator"}
    with TestClient(app) as client:
        yield client, insert


@pytest.mark.parametrize("path", ["/etc/private.png", "../../private.png", "/app/backend/static/other/thumb.png", ""])
def test_http_asset_path_is_rejected_without_persisting(http_boundary, path):
    client, insert = http_boundary
    response = client.post("/api/projects/project/assets", json={
        "name": "thumbnail", "asset_type": "generated_thumbnail", "file_path": path,
    })
    assert response.status_code == 422
    insert.assert_not_awaited()


@pytest.mark.parametrize("extra", [{}, {"file_path": None}])
def test_http_metadata_without_a_server_path_remains_supported(http_boundary, extra):
    client, insert = http_boundary
    response = client.post("/api/projects/project/assets", json={
        "name": "brief", "asset_type": "generated_brief", **extra,
    })
    assert response.status_code == 200
    assert response.json()["file_path"] is None
    insert.assert_awaited_once()
