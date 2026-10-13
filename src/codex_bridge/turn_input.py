from __future__ import annotations

from collections.abc import Sequence


def build_turn_input(prompt: str, image_paths: Sequence[str] = ()) -> list[dict[str, str]]:
    """Build the App Server input array without changing the text-only wire shape."""
    items = [{"type": "text", "text": prompt}]
    items.extend({"type": "localImage", "path": path} for path in image_paths)
    return items
