import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.storage import Storage


@contextmanager
def mock_upstream(handler):
    async_client = httpx.AsyncClient
    with patch("backend.responses_client.httpx.AsyncClient", side_effect=lambda **kwargs:
               async_client(transport=httpx.MockTransport(handler), **kwargs)):
        yield


def draft(**changes):
    return {
        "base_url": "https://draft.example/v1/", "models_path": "/models",
        "api_key": "draft-secret", "timeout_seconds": 15, **changes,
    }


class ModelsApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_root = Path(directory.name)

    def test_draft_models_request_and_settings_are_not_persisted(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "vision-b", "owned_by": "ignored"}, {"id": "vision-a"},
                {"id": "vision-b"},
            ] if len(requests) == 1 else []})

        with TestClient(create_app(self.data_root)) as client:
            client.put("/api/settings", json={"base_url": "https://saved.example/v1",
                                              "api_key": "saved-secret"})
            before = client.get("/api/settings").json()
            with mock_upstream(handler):
                custom = client.post("/api/settings/models", json=draft(models_path="catalog/models"))
                assert custom.status_code == 200
                assert custom.json() == {"models": ["vision-b", "vision-a"]}
                empty = client.post("/api/settings/models", json=draft(models_path=""))
                assert empty.status_code == 200
                assert empty.json() == {"models": []}
            assert [str(request.url) for request in requests] == [
                "https://draft.example/v1/catalog/models", "https://draft.example/v1",
            ]
            for request in requests:
                assert request.method == "GET"
                assert request.headers["Authorization"] == "Bearer draft-secret"
                assert request.content == b""
                assert request.extensions["timeout"]["read"] == 15
            assert client.get("/api/settings").json() == before
            assert client.app.state.secrets.api_key == "saved-secret"
        with TestClient(create_app(self.data_root)) as restarted:
            assert restarted.get("/api/settings").json() == before
            assert restarted.app.state.storage.get_api_key() == "saved-secret"


    def test_saved_key_only_reused_for_same_base_and_explicit_key_works(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"object": "list", "data": []})

        with TestClient(create_app(self.data_root)) as client, mock_upstream(handler):
            client.put("/api/settings", json={"base_url": "https://saved.example/v1",
                                              "api_key": "saved-secret"})
            same_base = draft(base_url="https://saved.example/v1/", api_key="  ")
            assert client.post("/api/settings/models", json=same_base).status_code == 200
            assert requests[-1].headers["Authorization"] == "Bearer saved-secret"
            for body in (draft(api_key=None), {**same_base, "clear_api_key": True}):
                response = client.post("/api/settings/models", json=body)
                assert response.status_code == 400
                assert "saved-secret" not in response.text
            assert len(requests) == 1
            assert client.post("/api/settings/models", json=draft()).status_code == 200
            assert requests[-1].headers["Authorization"] == "Bearer draft-secret"
            assert str(requests[-1].url) == "https://draft.example/v1/models"
            assert client.app.state.storage.get_api_key() == "saved-secret"


    def test_upstream_failures_are_502_without_echoing_body_or_following_redirect(self):
        marker = "upstream-private-detail-draft-secret"
        outcomes = [
            httpx.Response(401, text=marker),
            httpx.Response(302, headers={"Location": "https://redirect.example/"}, text=marker),
            httpx.Response(200, text=marker),
            httpx.Response(200, json={"object": "list", "data": [{"id": 3}], "detail": marker}),
            httpx.Response(200, json={"data": [], "detail": marker}),
            httpx.ConnectError(marker),
        ]
        requests = []

        def handler(request):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with TestClient(create_app(self.data_root)) as client, mock_upstream(handler):
            for count in range(6):
                response = client.post("/api/settings/models", json=draft())
                assert response.status_code == 502
                assert marker not in response.text
                assert "draft-secret" not in response.text
                assert len(requests) == count + 1
            assert all(request.url.host == "draft.example" for request in requests)


    def test_models_path_save_validation_and_legacy_settings_default(self):
        storage = Storage(self.data_root)
        storage.initialize()
        legacy = storage.get_settings()
        legacy.pop("models_path")
        with closing(sqlite3.connect(storage.db_path)) as connection:
            connection.execute("UPDATE settings SET value = ? WHERE id = 1", (json.dumps(legacy),))
            connection.commit()
        with TestClient(create_app(self.data_root)) as client:
            assert client.get("/api/settings").json()["models_path"] == "/models"
            for supplied, expected in (("catalog/models", "/catalog/models"), ("", "")):
                response = client.put("/api/settings", json={"models_path": supplied})
                assert response.status_code == 200
                assert response.json()["models_path"] == expected
                assert client.app.state.storage.get_settings()["models_path"] == expected
            for invalid in ("https://other.example/models", "/../models", "/models?token=x"):
                assert client.put("/api/settings", json={"models_path": invalid}).status_code == 422
                assert client.post("/api/settings/models", json=draft(models_path=invalid)).status_code == 422
        with TestClient(create_app(self.data_root)) as restarted:
            assert restarted.get("/api/settings").json()["models_path"] == ""

