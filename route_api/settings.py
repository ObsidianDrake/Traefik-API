import ipaddress
import os
from pathlib import Path
from typing import List, Optional, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, field_validator


class Settings(BaseModel):
    config_dir: Path
    config_format: Literal["yaml", "toml"] = "yaml"
    token: SecretStr
    entrypoint: str = Field(default="web", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    traefik_api_url: str = "http://127.0.0.1:8083/api"
    traefik_api_timeout: float = Field(default=5, gt=0, le=60)
    allowed_hosts: List[str] = Field(default_factory=list)
    allowed_cidrs: List[str] = Field(default_factory=list)
    probe_timeout: float = Field(default=2, gt=0, le=30)
    probe_attempts: int = Field(default=3, ge=1, le=5)
    probe_interval: float = Field(default=0.2, ge=0, le=5)
    max_body_bytes: int = Field(default=16384, ge=1024, le=1048576)

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        if len(value.get_secret_value()) < 24:
            raise ValueError("API_TOKEN must be at least 24 characters")
        return value

    @field_validator("traefik_api_url")
    @classmethod
    def valid_traefik_url(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0
                or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value):
            raise ValueError("TRAEFIK_API_URL must be an HTTP(S) API base URL without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("allowed_cidrs")
    @classmethod
    def valid_cidrs(cls, values):
        return [v if v == "*" else str(ipaddress.ip_network(v)) for v in values]

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None):
        path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
        file_values = {}
        if env_file is not None or path.exists():
            with path.open(encoding="utf-8-sig") as stream:
                # Treat secrets literally; do not execute shell or expand ${...}.
                file_values = {key: value for key, value in
                               dotenv_values(stream=stream, interpolate=False).items()
                               if value is not None}
        env = {**file_values, **os.environ}
        def items(name):
            return [item.strip().lower() for item in env.get(name, "").split(",") if item.strip()]
        return cls(
            config_dir=Path(env.get("TRAEFIK_CONFIG_DIR", "./data/config")),
            config_format=env.get("TRAEFIK_CONFIG_FORMAT", "yaml"),
            token=env.get("API_TOKEN", ""),
            entrypoint=env.get("TRAEFIK_ENTRYPOINT", "web"),
            traefik_api_url=env.get("TRAEFIK_API_URL", "http://127.0.0.1:8083/api"),
            traefik_api_timeout=env.get("TRAEFIK_API_TIMEOUT_SECONDS", "5"),
            allowed_hosts=items("ALLOWED_UPSTREAM_HOSTS"),
            allowed_cidrs=items("ALLOWED_UPSTREAM_CIDRS"),
            probe_timeout=env.get("PROBE_TIMEOUT_SECONDS", "2"),
            probe_attempts=env.get("PROBE_ATTEMPTS", "3"),
            probe_interval=env.get("PROBE_INTERVAL_SECONDS", "0.2"),
            max_body_bytes=env.get("MAX_BODY_BYTES", "16384"),
        )
