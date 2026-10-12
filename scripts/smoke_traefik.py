"""Exercise real Traefik file watching using isolated temporary config and backends.

Run from the repository root:
  venv/bin/python scripts/smoke_traefik.py --traefik /path/to/traefik
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from route_api.main import create_app
from route_api.settings import Settings


def backend(label):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = f"{label}:{self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def main(binary):
    servers, process = [], None
    with tempfile.TemporaryDirectory(prefix="traefik-route-smoke-") as temporary:
        root = Path(temporary)
        config = root / "dynamic"
        config.mkdir()
        try:
            first = backend("old")
            servers.append(first)
            second = backend("new")
            servers.append(second)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            static = root / "traefik.yml"
            static.write_text(
                f"entryPoints:\n  web:\n    address: '127.0.0.1:{port}'\n"
                f"providers:\n  providersThrottleDuration: 100ms\n  file:\n    directory: '{config}'\n    watch: true\n"
                "global:\n  checkNewVersion: false\n  sendAnonymousUsage: false\n")
            with (root / "traefik.log").open("w+") as log:
                process = subprocess.Popen([binary, f"--configFile={static}"], stdout=log, stderr=log)
                token = secrets.token_urlsafe(32)
                app = create_app(Settings(config_dir=config, token=token,
                                          allowed_cidrs=["127.0.0.0/8"], probe_interval=0.05))
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api",
                                                 headers={"Authorization": "Bearer " + token}) as api:
                        async with httpx.AsyncClient(trust_env=False, timeout=1) as browser:
                            async def eventually(path, status, text=None):
                                deadline = time.monotonic() + 10
                                while time.monotonic() < deadline:
                                    if process.poll() is not None:
                                        log.seek(0)
                                        raise AssertionError(log.read())
                                    try:
                                        response = await browser.get(f"http://127.0.0.1:{port}{path}")
                                        if response.status_code == status and (text is None or response.text == text):
                                            return
                                    except httpx.HTTPError:
                                        pass
                                    await asyncio.sleep(0.1)
                                log.seek(0)
                                raise AssertionError(f"route did not reach expected state: {path}\n{log.read()}")

                            body = {"pathPrefix": "/app", "stripPrefix": True,
                                    "upstreams": [f"http://127.0.0.1:{first.server_port}"]}
                            assert (await api.put("/v1/routes/demo", json=body)).status_code == 201
                            await eventually("/app/hello", 200, "old:/hello")
                            await eventually("/application", 404)
                            replacement = {**body, "upstreams": [f"http://127.0.0.1:{second.server_port}"]}
                            assert (await api.put("/v1/routes/demo", json=replacement)).status_code == 409
                            first.shutdown()
                            first.server_close()
                            servers.remove(first)
                            result = await api.put("/v1/routes/demo", json=replacement)
                            assert result.status_code == 200, result.text
                            await eventually("/app/hello", 200, "new:/hello")
                            assert (await api.delete("/v1/routes/demo")).status_code == 204
                            await eventually("/app/hello", 404)
                print("PASS: create, StripPrefix, path boundary, live rejection, offline takeover, delete; no Traefik restart")
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--traefik", required=True, help="Path to a Traefik v3 executable")
    asyncio.run(main(parser.parse_args().traefik))
