import asyncio

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from route_api.main import create_app
from route_api.settings import Settings
from route_api.traefik import fetch_routers

pytestmark = pytest.mark.anyio
TOKEN = "test-traefik-api-token-0123456789"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path):
    return Settings(config_dir=tmp_path, token=TOKEN, allowed_hosts=["backend"],
                    traefik_api_url="http://traefik:8083/api", traefik_api_timeout=0.5)


async def test_lists_live_routers_all_providers_and_pages(settings):
    calls = []
    pages = [
        [{"name": "manual@file", "rule": "PathPrefix(`/manual`)", "status": "enabled"},
         {"name": "demo@file", "status": "enabled", "service": "demo"}],
        [{"name": "demo@internal", "status": "enabled"},
         {"name": "dashboard@internal", "status": "enabled"},
         {"name": "broken@file", "status": "disabled", "error": ["missing service"]}],
    ]

    def handler(request):
        calls.append(request)
        assert "authorization" not in request.headers
        assert request.url.path == "/api/http/routers"
        assert request.url.params["per_page"] == "100"
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages[page - 1], headers={"X-Next-Page": "2" if page == 1 else "1"})

    app = create_app(settings, traefik_transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://api",
                                    headers={"Authorization": "Bearer " + TOKEN}) as client:
            body = {"pathPrefix": "/demo", "upstreams": ["http://backend:3000"]}
            assert (await client.put("/v1/routes/demo", json=body)).status_code == 201
            response = await client.get("/v1/routes")
            assert response.status_code == 200
            routes = response.json()["routes"]
            assert len(routes) == 5
            assert routes[0]["managed"] is False and routes[0]["managedName"] is None
            assert routes[1]["managed"] is True and routes[1]["managedName"] == "demo"
            assert routes[2]["managed"] is False
            assert routes[4]["status"] == "disabled"
            assert routes[4]["error"] == ["missing service"]
            # Each request re-fetches Traefik instead of using local files or cached results.
            await client.get("/v1/routes")
            assert len(calls) == 4
            denied = await client.get("/v1/routes", headers={"Authorization": "Bearer invalid"})
            assert denied.status_code == 401 and len(calls) == 4
            assert (await client.delete("/v1/routes/manual")).status_code == 404


@pytest.mark.parametrize("status", [301, 401, 403, 404, 500])
async def test_http_failures_are_not_empty_success(settings, status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="private error", headers={"Location": "http://other/"}))) as client:
        with pytest.raises(HTTPException) as caught:
            await fetch_routers(client, settings)
        assert caught.value.status_code == 502
        assert caught.value.detail == {"code": "traefik_api_error", "traefikStatus": status}


@pytest.mark.parametrize("body,headers", [
    ({"routers": []}, {}), ([{}], {}), ([{"name": 42}], {}),
    ([], {"X-Next-Page": "0"}), ([], {"X-Next-Page": "-1"}),
    ([], {"X-Next-Page": "http://other/"}),
])
async def test_invalid_response_or_pagination(settings, body, headers):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body, headers=headers))) as client:
        with pytest.raises(HTTPException) as caught:
            await fetch_routers(client, settings)
        assert caught.value.status_code == 502
        assert caught.value.detail["code"] == "traefik_api_invalid_response"


@pytest.mark.parametrize("entries", [[], [{"name": "manual@file", "status": "enabled"}]])
async def test_single_page_traefik_end_marker(settings, entries):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=entries, headers={"X-Next-Page": "1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_routers(client, settings) == entries
    assert len(calls) == 1


@pytest.mark.parametrize("last_next_page", ["2", "3"])
async def test_nonterminal_repeated_or_backward_page_rejected(settings, last_next_page):
    def handler(request):
        page = int(request.url.params["page"])
        next_page = str(page + 1) if page < 3 else last_next_page
        return httpx.Response(200, json=[{"name": f"router-{page}@file"}],
                              headers={"X-Next-Page": next_page})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as caught:
            await fetch_routers(client, settings)
        assert caught.value.status_code == 502


@pytest.mark.parametrize("mode,expected", [("connect", 502), ("timeout", 504), ("slow", 504)])
async def test_network_errors_and_total_deadline(settings, mode, expected):
    settings.traefik_api_timeout = 0.01

    async def handler(request):
        if mode == "connect":
            raise httpx.ConnectError("unreachable", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        await asyncio.sleep(1)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as caught:
            await fetch_routers(client, settings)
        assert caught.value.status_code == expected


async def test_failed_later_page_does_not_return_partial_list(settings):
    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"name": "first@file"}], headers={"X-Next-Page": "2"})
        return httpx.Response(200, text="not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as caught:
            await fetch_routers(client, settings)
        assert caught.value.status_code == 502


@pytest.mark.parametrize("url", ["file:///tmp/api", "http://user:pass@traefik/api",
                               "http://traefik/api?x=1", "http://traefik:99999/api"])
async def test_invalid_api_url_rejected(settings, url):
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), "traefik_api_url": url})


async def test_api_settings_loaded_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("API_TOKEN", "TRAEFIK_API_URL", "TRAEFIK_API_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(f"API_TOKEN={TOKEN}\nTRAEFIK_API_URL=http://example:8080/prefix/api/\nTRAEFIK_API_TIMEOUT_SECONDS=3\n")
    config = Settings.from_env()
    assert config.traefik_api_url == "http://example:8080/prefix/api"
    assert config.traefik_api_timeout == 3
