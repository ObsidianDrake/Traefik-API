"""Local CLI entry point, also used by the standalone PyInstaller build."""
import argparse
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from route_api.main import create_app
from route_api.settings import Settings


def main():
    parser = argparse.ArgumentParser(description="Local Traefik route registration API")
    parser.add_argument("--host", default="127.0.0.1", help="Listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Listen port (default: 8000)")
    parser.add_argument("--env-file", type=Path,
                        help="Settings file (default: .env in the current working directory)")
    parser.add_argument("--check-config", action="store_true",
                        help="Validate environment settings without opening sockets or writing files")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        app = create_app(Settings.from_env(args.env_file))
    except (OSError, UnicodeError):
        parser.error("cannot read env file; check its path, permissions and UTF-8 encoding")
    except ValidationError as exc:
        # Omit input values, especially API_TOKEN, from validation output.
        errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        parser.error(errors)
    if args.check_config:
        print("Environment settings valid. Filesystem, Traefik and upstream connectivity were not checked.")
        return
    uvicorn.run(app, host=args.host, port=args.port, workers=1,
                loop="asyncio", http="h11", ws="none", lifespan="on")


if __name__ == "__main__":
    main()
