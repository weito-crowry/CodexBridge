from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence
from typing import Any

from .console.config import ConsoleConfig, ConsoleConfigurationError

_MISSING_EXTRA_MESSAGE = (
    "CodexBridge Console requires the 'console' extra.\nInstall with: uv sync --extra console"
)


class ConsoleDependencyError(RuntimeError):
    """Raised when the optional GUI dependency is not installed."""


def _load_gui() -> tuple[type[Any], type[Any]]:
    try:
        from PySide6.QtWidgets import QApplication

        from .console.main_window import MainWindow
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6" or (exc.name and exc.name.startswith("PySide6.")):
            raise ConsoleDependencyError(_MISSING_EXTRA_MESSAGE) from None
        raise
    return QApplication, MainWindow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-bridge-console")
    parser.add_argument("--ui-port", type=str, default=None)
    parser.add_argument("--allowed-root", action="append", default=None)
    parser.add_argument("--codex-executable", default=None)
    parser.add_argument("--tunnel-executable", default=None)
    parser.add_argument("--tunnel-profile", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        explicit_port = None if args.ui_port is None else args.ui_port
        config = ConsoleConfig.from_sources(
            explicit_port=explicit_port,
            explicit_allowed_roots=(
                None if args.allowed_root is None else tuple(args.allowed_root)
            ),
            explicit_codex_executable=args.codex_executable,
            explicit_tunnel_executable=args.tunnel_executable,
            explicit_tunnel_profile=args.tunnel_profile,
        )
        QApplication, MainWindow = _load_gui()
    except (ConsoleConfigurationError, ConsoleDependencyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    application = QApplication([sys.argv[0]])
    window = MainWindow(config)
    window.show()
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_args: window.request_sigint())
    try:
        return int(application.exec())
    finally:
        signal.signal(signal.SIGINT, previous_sigint)


if __name__ == "__main__":
    raise SystemExit(main())
