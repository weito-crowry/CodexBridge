from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from .config import BridgeConfig
from .logging_utils import configure_logging
from .server import run_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-bridge")
    parser.add_argument("--allowed-root", action="append", default=None)
    parser.add_argument("--codex-executable", default=None)
    parser.add_argument("--ui-port", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        config = BridgeConfig.from_sources(
            explicit_allowed_roots=None if args.allowed_root is None else tuple(args.allowed_root),
            explicit_codex_executable=args.codex_executable,
            explicit_ui_port=args.ui_port,
        )
        asyncio.run(run_server(config))
    except (ValueError, OSError) as exc:
        logging.getLogger("codex_bridge").error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logging.getLogger("codex_bridge").info("bridge.shutdown.interrupted")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
