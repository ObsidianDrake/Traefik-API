import ipaddress
import re
from typing import List, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
HOST_PATTERN = r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"


def safe_path(value: str) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9/_~.-]*", value):
        raise ValueError("path must start with / and contain only letters, digits, / _ ~ . -")
    if "//" in value or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("ambiguous path segments are not allowed")
    return value


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: Optional[str] = None
    pathPrefix: Optional[str] = None
    stripPrefix: bool = False
    upstreams: List[str] = Field(min_length=1, max_length=16)
    probePath: str = "/"

    @field_validator("host")
    @classmethod
    def host_valid(cls, value):
        if value is None:
            return value
        value = value.lower().rstrip(".")
        if not re.fullmatch(HOST_PATTERN, value):
            raise ValueError("host must be a DNS hostname without scheme, port or wildcard")
        return value

    @field_validator("pathPrefix")
    @classmethod
    def prefix_valid(cls, value):
        return (safe_path(value).rstrip("/") or "/") if value is not None else None

    @field_validator("probePath")
    @classmethod
    def probe_valid(cls, value):
        return safe_path(value)

    @field_validator("upstreams")
    @classmethod
    def urls_valid(cls, values):
        result = []
        for value in values:
            if len(value) > 2048 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
                raise ValueError("invalid upstream URL")
            parsed = urlsplit(value)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
                raise ValueError("upstream must be an http(s) origin, without credentials, path, query or fragment")
            # Accessing port also validates its syntax and range.
            if parsed.port == 0:
                raise ValueError("invalid upstream port")
            try:
                ipaddress.ip_address(parsed.hostname)
            except ValueError:
                if not re.fullmatch(HOST_PATTERN, parsed.hostname):
                    raise ValueError("upstream hostname must be a DNS name or IP address")
            result.append(value.rstrip("/"))
        if len(set(result)) != len(result):
            raise ValueError("duplicate upstreams")
        return result

    @model_validator(mode="after")
    def valid_combination(self):
        if not self.host and not self.pathPrefix:
            raise ValueError("host or pathPrefix is required")
        if self.stripPrefix and self.pathPrefix in (None, "/"):
            raise ValueError("stripPrefix requires a non-root pathPrefix")
        return self

    def key(self):
        return self.host, self.pathPrefix or "/"


def rule_for(route: Route) -> str:
    parts = []
    if route.host:
        parts.append(f"Host(`{route.host}`)")
    if route.pathPrefix == "/":
        parts.append("PathPrefix(`/`)")
    elif route.pathPrefix:
        path = route.pathPrefix
        parts.append(f"(Path(`{path}`) || PathPrefix(`{path}/`))")
    return " && ".join(parts)


def render(name: str, route: Route, entrypoint: str) -> dict:
    router = {"rule": rule_for(route), "entryPoints": [entrypoint], "service": name}
    http = {"routers": {name: router}, "services": {name: {
        "loadBalancer": {"servers": [{"url": url} for url in route.upstreams]}
    }}}
    if route.stripPrefix:
        middleware = f"{name}-strip"
        router["middlewares"] = [middleware]
        http["middlewares"] = {middleware: {"stripPrefix": {"prefixes": [route.pathPrefix]}}}
    return {"http": http}
