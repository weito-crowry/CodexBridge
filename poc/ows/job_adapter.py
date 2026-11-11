from __future__ import annotations

import sys

from _product_imports import prepare_product_import

prepare_product_import()

from codex_bridge.workflows import job_adapter as _implementation  # noqa: E402

globals().update(
    {name: value for name, value in vars(_implementation).items() if not name.startswith("__")}
)
if __name__ in sys.modules and __name__ != "__main__":
    sys.modules[__name__] = _implementation
