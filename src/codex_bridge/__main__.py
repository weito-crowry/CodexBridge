from __future__ import annotations

import asyncio

from .config import BridgeConfig
from .server import run_server


def main() -> None:
    asyncio.run(run_server(BridgeConfig.from_env()))


if __name__ == "__main__":
    main()
