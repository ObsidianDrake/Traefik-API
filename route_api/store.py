import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import tomli
import toml
import yaml
from fastapi import HTTPException

from .models import NAME_PATTERN, Route, render

MARKER = "# traefik-route-api:v2 "


def encode(name, route, entrypoint, config_format="yaml"):
    metadata = {"name": name, "entrypoint": entrypoint, "route": route.model_dump()}
    doc = render(name, route, entrypoint)
    content = toml.dumps(doc) if config_format == "toml" else yaml.safe_dump(doc, sort_keys=False)
    return MARKER + json.dumps(metadata, separators=(",", ":")) + "\n" + content


class Store:
    def __init__(self, directory: Path, entrypoint: str, config_format="yaml"):
        self.directory = directory
        self.entrypoint = entrypoint
        self.config_format = config_format
        self.paths = {}
        self.lock_file = None

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / ".route-api.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock_file = os.fdopen(fd, "a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.scan()
        except Exception:
            self.close()
            raise

    def close(self):
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    def scan(self):
        managed, external, hashes = {}, [], {}
        paths = {}
        for path in sorted(self.directory.rglob("*")):
            if path.is_symlink():
                raise HTTPException(503, {"code": "symlink_in_config", "file": path.name})
            if not path.is_file() or path.suffix.lower() not in (".yml", ".yaml", ".toml"):
                continue
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("config too large")
                raw = path.read_bytes()
                hashes[str(path)] = hashlib.sha256(raw).hexdigest()
                text = raw.decode()
                doc = tomli.loads(text) if path.suffix.lower() == ".toml" else yaml.safe_load(text)
                if not isinstance(doc, dict):
                    raise ValueError("config must be a mapping")
                http = doc.get("http", {})
                if not isinstance(http, dict):
                    raise ValueError("http must be a mapping")
                for group in ("routers", "services", "middlewares"):
                    entries = http.get(group, {})
                    if not isinstance(entries, dict) or any(not isinstance(v, dict) for v in entries.values()):
                        raise ValueError("http sections must contain object mappings")
                for router in http.get("routers", {}).values():
                    if (not isinstance(router.get("rule", ""), str)
                            or not isinstance(router.get("entryPoints", []), list)):
                        raise ValueError("invalid router")
                if text.startswith(MARKER):
                    meta = json.loads(text.splitlines()[0][len(MARKER):])
                    name = meta["name"]
                    route = Route.model_validate(meta["route"])
                    expected_paths = [self.directory / f"{name}.yml", self.directory / f"{name}.toml"]
                    if (not isinstance(name, str) or not re.fullmatch(NAME_PATTERN, name)
                            or path not in expected_paths or name in managed
                            or meta["entrypoint"] != self.entrypoint
                            or doc != render(name, route, self.entrypoint)):
                        raise ValueError("managed file was changed externally")
                    managed[name] = route
                    paths[name] = path
                else:
                    external.append((path.name, doc))
            except (ValueError, KeyError, TypeError, yaml.YAMLError, UnicodeError) as exc:
                raise HTTPException(503, {"code": "invalid_config", "file": path.name}) from exc
        self.paths = paths
        return managed, external, hashes

    def path_for(self, name):
        suffix = "toml" if self.config_format == "toml" else "yml"
        return self.paths.get(name, self.directory / f"{name}.{suffix}")

    def check_filename(self, name):
        destination = self.path_for(name)
        # Do not create the same name alongside an unmanaged file in another format.
        for path in {destination, *(self.directory / f"{name}.{ext}" for ext in ("yml", "yaml", "toml"))}:
            if path.exists() and path != self.paths.get(name):
                raise HTTPException(409, {"code": "unmanaged_file", "file": path.name})

    def check_external(self, name, route, external):
        """Recognize literal Host/Path/PathPrefix conflicts, conservatively on overlap."""
        for filename, doc in external:
            http = doc.get("http") or {}
            if not isinstance(http, dict):
                raise HTTPException(503, {"code": "invalid_config", "file": filename})
            for group in ("routers", "services", "middlewares"):
                if name in (http.get(group) or {}) or f"{name}-strip" in (http.get(group) or {}):
                    raise HTTPException(409, {"code": "manual_name_conflict", "file": filename})
            for router in (http.get("routers") or {}).values():
                entrypoints = router.get("entryPoints", [])
                if entrypoints and self.entrypoint not in entrypoints:
                    continue
                rule = router.get("rule", "")
                hosts = re.findall(r'Host\(\s*[`"]([^`"]+)[`"]\s*\)', rule)
                paths = re.findall(r'Path(?:Prefix)?\(\s*[`"]([^`"]+)[`"]\s*\)', rule)
                host_overlap = not hosts or route.host is None or route.host in [h.lower() for h in hosts]
                path_overlap = not paths or route.pathPrefix is None or any(
                    route.pathPrefix.startswith(p.rstrip("/") or "/")
                    or p.startswith(route.pathPrefix) for p in paths)
                if (hosts or paths) and host_overlap and path_overlap:
                    raise HTTPException(409, {"code": "manual_route_conflict", "file": filename})

    def write(self, name, route):
        self.check_filename(name)
        destination = self.path_for(name)
        config_format = "toml" if destination.suffix == ".toml" else "yaml"
        fd, temporary = tempfile.mkstemp(prefix=".route-api-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as file:
                file.write(encode(name, route, self.entrypoint, config_format))
                file.flush()
                os.fchmod(file.fileno(), 0o644)
                os.fsync(file.fileno())
            os.replace(temporary, destination)
            self.sync_directory()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def delete(self, name):
        self.paths[name].unlink()
        self.sync_directory()

    def sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
