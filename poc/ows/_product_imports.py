from __future__ import annotations

import sys
from pathlib import Path


def prepare_product_import() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    if source_root.is_dir() and str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))


prepare_product_import()
