from __future__ import annotations

import argparse
import ctypes
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO, cast

from .console.config import ConsoleConfig, ConsoleConfigurationError
from .runtime_mode import INTERNAL_RUNTIME_FLAG

_MISSING_EXTRA_MESSAGE = (
    "CodexBridge Console requires the 'console' extra.\nInstall with: uv sync --extra console"
)
_ICON_PATH = Path(__file__).resolve().parent / "assets" / "codexbridge_icon_256.ico"
_APP_USER_MODEL_ID = "CodexBridge.Console"


class ConsoleDependencyError(RuntimeError):
    """Raised when the optional GUI dependency is not installed."""


class _NullTextStream:
    encoding = "utf-8"
    errors = "strict"
    line_buffering = True

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def writable(self) -> bool:
        return True


def _runtime_stdio_log_paths() -> tuple[Path, Path]:
    from .observability import bridge_runtime_stderr_log_path, bridge_runtime_stdout_log_path

    return bridge_runtime_stdout_log_path(), bridge_runtime_stderr_log_path()


def _prepare_internal_runtime_stdio() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return

    stdout_path: Path | None
    stderr_path: Path | None
    try:
        stdout_path, stderr_path = _runtime_stdio_log_paths()
    except Exception:
        stdout_path = None
        stderr_path = None

    for stream_name, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        if getattr(sys, stream_name) is not None:
            continue
        stream: TextIO
        try:
            if path is None:
                raise OSError("runtime log path unavailable")
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("a", encoding="utf-8", buffering=1)
        except Exception:
            stream = cast(TextIO, _NullTextStream())
        setattr(sys, stream_name, stream)


def _run_internal_bridge_runtime() -> int:
    from .__main__ import main as bridge_main

    return int(bridge_main([]))


def _load_gui() -> tuple[type[Any], type[Any]]:
    try:
        from PySide6.QtWidgets import QApplication

        from .console.main_window import MainWindow
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6" or (exc.name and exc.name.startswith("PySide6.")):
            raise ConsoleDependencyError(_MISSING_EXTRA_MESSAGE) from None
        raise
    return QApplication, MainWindow


def _load_application_icon() -> Any:
    from PySide6.QtGui import QIcon

    return QIcon(str(_ICON_PATH))


def _set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        windll = ctypes.windll
        shell32 = windll.shell32
        set_app_id = shell32.SetCurrentProcessExplicitAppUserModelID
        if callable(set_app_id):
            set_app_id(_APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        return


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-bridge-console")
    parser.add_argument("--ui-port", type=str, default=None)
    parser.add_argument("--allowed-root", action="append", default=None)
    parser.add_argument("--codex-executable", default=None)
    parser.add_argument("--tunnel-executable", default=None)
    parser.add_argument("--tunnel-profile", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == [INTERNAL_RUNTIME_FLAG]:
        _prepare_internal_runtime_stdio()
        return _run_internal_bridge_runtime()

    args = _parser().parse_args(arguments)
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

    _set_windows_app_user_model_id()
    application = QApplication([sys.argv[0]])
    application.setWindowIcon(_load_application_icon())
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
