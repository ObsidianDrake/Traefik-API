"""Read the running Traefik instance's HTTP routers, including all pages."""
import asyncio
import json

import httpx
from fastapi import HTTPException


async def fetch_routers(client, settings):
    async def fetch_all():
        routers = {}
        page = 1
        remaining_bytes = 10 * 1024 * 1024
        for _ in range(1000):
            async with client.stream(
                "GET", settings.traefik_api_url + "/http/routers",
                params={"page": page, "per_page": 100},
                timeout=settings.traefik_api_timeout,
            ) as response:
                if response.status_code != 200:
                    raise HTTPException(502, {"code": "traefik_api_error",
                                              "traefikStatus": response.status_code})
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining_bytes -= len(chunk)
                    if remaining_bytes < 0:
                        raise ValueError("response too large")
                    body.extend(chunk)
                entries = json.loads(body)
                if not isinstance(entries, list):
                    raise ValueError("expected router list")
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
                        raise ValueError("invalid router")
                    routers[entry["name"]] = entry
                next_page = response.headers.get("X-Next-Page", "").strip()
                if not next_page:
                    return list(routers.values())
                new_page = int(next_page)
                # Traefik points back to page 1 when there are no more pages.
                if new_page == 1:
                    return list(routers.values())
                if new_page <= page:
                    raise ValueError("invalid pagination")
                page = new_page
        raise ValueError("too many pages")

    try:
        return await asyncio.wait_for(fetch_all(), timeout=settings.traefik_api_timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(504, {"code": "traefik_api_timeout"}) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, {"code": "traefik_api_unavailable"}) from exc
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(502, {"code": "traefik_api_invalid_response"}) from exc
