import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Path, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import NAME_PATTERN, Route
from .probe import check_allowed, probe_route
from .settings import Settings
from .store import Store, encode
from .traefik import fetch_routers

logger = logging.getLogger("uvicorn.error")


class BodyLimit:
    """Bound bodies even when Content-Length is absent or false."""
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            try:
                message = await asyncio.wait_for(receive(), timeout=10)
            except asyncio.TimeoutError:
                return await JSONResponse({"detail": "request body timed out"}, 408)(scope, receive, send)
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                return await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)


def create_app(settings: Optional[Settings] = None, transport=None, traefik_transport=None):
    settings = settings or Settings.from_env()
    store = Store(settings.config_dir, settings.entrypoint, settings.config_format)
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        store.start()
        try:
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
                app.state.probe_client = client
                async with httpx.AsyncClient(transport=traefik_transport, trust_env=False,
                                             follow_redirects=False) as traefik_client:
                    app.state.traefik_client = traefik_client
                    yield
        finally:
            store.close()

    app = FastAPI(title="Traefik Route API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)
    app.state.store = store
    bearer = HTTPBearer(auto_error=False)

    async def authenticated(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
        if (credentials is None or credentials.scheme.lower() != "bearer"
                or not secrets.compare_digest(credentials.credentials.encode(), settings.token.get_secret_value().encode())):
            raise HTTPException(401, "invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(authenticated)]

    def record(event, **fields):
        logger.info(json.dumps({"event": event, **fields}, separators=(",", ":")))

    def item(name, route):
        return {"name": name, "entryPoint": settings.entrypoint, "route": route.model_dump()}

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        logger.error("route storage operation failed: %s", type(exc).__name__)
        return JSONResponse({"detail": {"code": "storage_unavailable"}}, status_code=503)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/routes/preview", dependencies=auth)
    async def preview(route: Route):
        check_allowed(route, settings)
        return {"format": settings.config_format,
                settings.config_format: encode("preview", route, settings.entrypoint, settings.config_format)}

    @app.get("/v1/routes", dependencies=auth,
             summary="List all HTTP routers currently reported by Traefik",
             responses={502: {"description": "Traefik API unavailable or invalid response"},
                        504: {"description": "Traefik API timeout"}})
    async def list_routes():
        routers = await fetch_routers(app.state.traefik_client, settings)
        async with lock:
            managed, _, _ = store.scan()
        ownership = {f"{key}@file": key for key in managed}
        return {"routes": [{**router, "managed": router["name"] in ownership,
                            "managedName": ownership.get(router["name"])} for router in routers]}

    @app.get("/v1/routes/{name}", dependencies=auth)
    async def get_route(name: str = Path(pattern=NAME_PATTERN)):
        async with lock:
            managed, _, _ = store.scan()
            if name not in managed:
                raise HTTPException(404, "route not found")
            return item(name, managed[name])

    @app.put("/v1/routes/{name}", dependencies=auth)
    async def put_route(route: Route, response: Response, name: str = Path(pattern=NAME_PATTERN)):
        check_allowed(route, settings)
        async with lock:
            managed, external, before = store.scan()
            # The entry owns a canonical name. Different callers cannot create duplicate entries.
            owners = [key for key, value in managed.items() if value.key() == route.key()]
            targets = set(owners + ([name] if name in managed else []))
            if len(targets) > 1:
                raise HTTPException(409, {"code": "multiple_route_conflicts", "names": sorted(targets)})
            effective_name = next(iter(targets), name)
            store.check_filename(effective_name)
            store.check_external(effective_name, route, external)
            previous = managed.get(effective_name)
            probes = []
            if previous is not None:
                probes = await probe_route(previous, settings, app.state.probe_client)
                if any(p["state"] == "responding" for p in probes):
                    record("route_rejected", name=effective_name, probes=probes)
                    raise HTTPException(409, {"code": "existing_service_responding", "name": effective_name, "probes": probes})
                if any(p["state"] != "unreachable" for p in probes):
                    record("probe_inconclusive", name=effective_name, probes=probes)
                    raise HTTPException(503, {"code": "probe_inconclusive", "name": effective_name, "probes": probes})
            if store.scan()[2] != before:
                raise HTTPException(409, {"code": "config_changed", "message": "configuration changed during probe; retry"})
            store.write(effective_name, route)
            record("route_written", name=effective_name, requestedName=name,
                   previous=previous.model_dump() if previous else None, new=route.model_dump(), probes=probes)
            response.status_code = 200 if previous else 201
            return {**item(effective_name, route), "requestedName": name, "status": "written", "probes": probes}

    @app.delete("/v1/routes/{name}", status_code=204, dependencies=auth)
    async def delete_route(name: str = Path(pattern=NAME_PATTERN)):
        async with lock:
            managed, _, _ = store.scan()
            if name not in managed:
                raise HTTPException(404, "route not found")
            store.delete(name)
            record("route_deleted", name=name, previous=managed[name].model_dump())
        return Response(status_code=204)

    return app
