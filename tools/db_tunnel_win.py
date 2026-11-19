"""Optional standalone tunnel runner using the application's .env settings.

Normally start the backend instead: it owns and supervises these tunnels.
Do not run both at the same time because their local ports would conflict.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.db_tunnel import db_tunnel  # noqa: E402
from app.manifest_tunnels import manifest_tunnels  # noqa: E402


async def main():
    async with manifest_tunnels(), db_tunnel(load_settings()):
        print("Tunnel manager running. Press Ctrl+C to stop.", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
