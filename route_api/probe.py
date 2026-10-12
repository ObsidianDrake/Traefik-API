import asyncio
import errno
import ipaddress
import ssl
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .models import Route
from .settings import Settings


def check_allowed(route: Route, settings: Settings):
    """Host * allows all origins; CIDR * allows literal IPs only."""
    for url in route.upstreams:
        host = urlsplit(url).hostname
        if "*" in settings.allowed_hosts or host in settings.allowed_hosts:
            continue
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and ("*" in settings.allowed_cidrs or any(
                address in ipaddress.ip_network(cidr) for cidr in settings.allowed_cidrs)):
            continue
        raise HTTPException(422, {"code": "upstream_not_allowed", "host": host})


def is_refused(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ssl.SSLError):
            return False
        if isinstance(error, OSError) and error.errno == errno.ECONNREFUSED:
            return True
        error = error.__cause__ or error.__context__
    return False


async def probe_route(route: Route, settings: Settings, client: httpx.AsyncClient):
    check_allowed(route, settings)

    async def one(origin):
        url = origin + route.probePath
        attempts = []
        for number in range(settings.probe_attempts):
            try:
                # Stream headers only. A huge or never-ending body is still a response.
                async def get_headers():
                    async with client.stream("GET", url, timeout=settings.probe_timeout,
                                             follow_redirects=False) as response:
                        return response.status_code
                status = await asyncio.wait_for(get_headers(), settings.probe_timeout)
                return {"upstream": origin, "state": "responding", "statusCode": status, "attempts": attempts}
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, asyncio.TimeoutError):
                attempts.append("timeout")
            except httpx.ConnectError as exc:
                if not is_refused(exc):
                    return {"upstream": origin, "state": "unknown", "reason": "connection_error"}
                attempts.append("connection_refused")
            except httpx.HTTPError:
                return {"upstream": origin, "state": "unknown", "reason": "protocol_or_network_error"}
            if number + 1 < settings.probe_attempts:
                await asyncio.sleep(settings.probe_interval)
        return {"upstream": origin, "state": "unreachable", "attempts": attempts}

    return await asyncio.gather(*(one(url) for url in route.upstreams))
