from __future__ import annotations

import os
import subprocess
import sys


def test_copy_to_clipboard_transfers_text_in_offscreen_qt_process() -> None:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "from PySide6.QtGui import QClipboard",
                    "from PySide6.QtWidgets import QApplication",
                    "from codex_bridge.console.widgets import copy_to_clipboard",
                    "application = QApplication([])",
                    "assert isinstance(application, QApplication)",
                    "assert application.platformName() == 'offscreen'",
                    "expected_text = 'clipboard-transfer-check'",
                    "copy_to_clipboard(expected_text)",
                    "clipboard = application.clipboard()",
                    "assert isinstance(clipboard, QClipboard)",
                    "assert clipboard.text() == expected_text",
                )
            ),
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        "offscreen Qt clipboard subprocess failed\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
