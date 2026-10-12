from contextlib import asynccontextmanager

import httpx
import pytest
import tomli
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from route_api.main import create_app
from route_api.models import Route
from route_api.settings import Settings
from route_api.store import encode

pytestmark = pytest.mark.anyio
TOKEN = "format-test-token-0123456789"
BODY = {"host": "apps.internal", "pathPrefix": "/app", "stripPrefix": True,
        "upstreams": ["http://old:3000", "http://new:3000"]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(params=["yaml", "toml"])
def settings(tmp_path, request):
    return Settings(config_dir=tmp_path, token=TOKEN, config_format=request.param,
                    allowed_hosts=["old", "new"], probe_attempts=1)


@asynccontextmanager
async def client_for(settings, alive=False):
    def probe(request):
        if alive:
            return httpx.Response(200)
        raise httpx.ConnectTimeout("offline", request=request)

    app = create_app(settings, transport=httpx.MockTransport(probe))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",
                                    headers={"Authorization": "Bearer " + TOKEN}) as client:
            yield client


def route_path(settings):
    return settings.config_dir / ("demo.toml" if settings.config_format == "toml" else "demo.yml")


async def test_format_crud_restart_and_name_contract(settings):
    async with client_for(settings) as client:
        preview = (await client.post("/v1/routes/preview", json=BODY)).json()
        assert preview["format"] == settings.config_format
        assert settings.config_format in preview
        assert not list(settings.config_dir.glob("*.yml"))
        assert not list(settings.config_dir.glob("*.toml"))
        response = await client.put("/v1/routes/demo", json=BODY)
        assert response.status_code == 201
        assert response.json()["name"] == response.json()["requestedName"] == "demo"
        assert "id" not in response.json()
        raw = route_path(settings).read_text()
        doc = tomli.loads(raw) if settings.config_format == "toml" else yaml.safe_load(raw)
        assert doc["http"]["routers"]["demo"]["entryPoints"] == ["web"]
        assert doc["http"]["services"]["demo"]["loadBalancer"]["servers"] == [
            {"url": "http://old:3000"}, {"url": "http://new:3000"}]
        assert doc["http"]["middlewares"]["demo-strip"]["stripPrefix"]["prefixes"] == ["/app"]
        schema = (await client.get("/openapi.json")).json()
        assert "/v1/routes/{name}" in schema["paths"]
        assert "/v1/routes/{route_id}" not in schema["paths"]
    original = route_path(settings)
    settings.config_format = "yaml" if settings.config_format == "toml" else "toml"
    async with client_for(settings) as client:
        assert (await client.get("/v1/routes/demo")).json()["name"] == "demo"
        updated = await client.put("/v1/routes/alias", json={**BODY, "upstreams": ["http://new:3000"]})
        assert updated.status_code == 200
        assert updated.json()["name"] == "demo"
        assert updated.json()["requestedName"] == "alias"
        assert original.exists()
        assert not route_path(settings).exists()
        assert (await client.delete("/v1/routes/demo")).status_code == 204
        assert not original.exists()


async def test_live_service_preserves_each_format(settings):
    async with client_for(settings, alive=True) as client:
        await client.put("/v1/routes/demo", json=BODY)
        before = route_path(settings).read_bytes()
        assert (await client.put("/v1/routes/demo", json=BODY)).status_code == 409
        assert route_path(settings).read_bytes() == before


@pytest.mark.parametrize("suffix", ["yml", "yaml", "toml"])
async def test_manual_filename_collision_in_any_format(settings, suffix):
    path = settings.config_dir / f"demo.{suffix}"
    content = "[http]\n" if suffix == "toml" else "http: {}\n"
    path.write_text(content)
    async with client_for(settings) as client:
        response = await client.put("/v1/routes/demo", json=BODY)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "unmanaged_file"
        assert path.read_text() == content


async def test_duplicate_managed_name_fails_closed(settings):
    for suffix, format_name in [("yml", "yaml"), ("toml", "toml")]:
        (settings.config_dir / f"demo.{suffix}").write_text(encode("demo", Route(**BODY), "web", format_name))
    with pytest.raises(HTTPException) as caught:
        async with client_for(settings):
            pass
    assert caught.value.status_code == 503


async def test_failed_atomic_write_keeps_original(settings, monkeypatch):
    async with client_for(settings) as client:
        await client.put("/v1/routes/demo", json=BODY)
        before = route_path(settings).read_bytes()

        def fail(*args):
            raise OSError("disk failure")

        monkeypatch.setattr("route_api.store.os.replace", fail)
        assert (await client.put("/v1/routes/demo", json=BODY)).status_code == 503
        assert route_path(settings).read_bytes() == before


async def test_format_loaded_and_validated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRAEFIK_CONFIG_FORMAT", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    (tmp_path / ".env").write_text(f"API_TOKEN={TOKEN}\nTRAEFIK_CONFIG_FORMAT=toml\n")
    assert Settings.from_env().config_format == "toml"
    monkeypatch.setenv("TRAEFIK_CONFIG_FORMAT", "xml")
    with pytest.raises(ValidationError):
        Settings.from_env()
