from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def allowed_dir(tmp_path):
    return tmp_path


def write_rollout(path: Path, *records: dict[str, Any]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
