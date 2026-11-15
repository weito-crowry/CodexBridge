from __future__ import annotations

import sys

from _product_imports import prepare_product_import

prepare_product_import()

from codex_bridge.workflows import job_service as _implementation  # noqa: E402

globals().update(
    {name: value for name, value in vars(_implementation).items() if not name.startswith("__")}
)

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
