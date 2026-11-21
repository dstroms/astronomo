"""Frames taken during a mission: the first-frame view and every FITS frame saved after it.

The dashboard only reads the mission folder.  A FITS frame is stretched the same way as the first-frame
view (``core.setup_checks.display_view``) so a person can see the picture.  The stretch is a view, not a
measurement.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from core.setup_checks import display_view
from tools.imaging import load_image

_PREVIEW_CACHE: dict[tuple, np.ndarray] = {}


def list_mission_frames(mission_dir: Path) -> list[dict]:
    """The first-frame picture, then each ``frame_*.fits`` under ``frames/``, in path order.

    Each item is ``{"label", "path", "kind"}`` with ``kind`` ``preview`` or ``fits``.
    """
    mission_dir = Path(mission_dir)
    found: list[dict] = []
    preview = mission_dir / "first_frame.png"
    if preview.is_file():
        found.append({"label": "First frame", "path": preview, "kind": "preview"})
    root = mission_dir / "frames"
    if root.is_dir():
        paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".fits", ".fit"})
        for path in paths:
            found.append({"label": str(path.relative_to(root)), "path": path, "kind": "fits"})
    return found


def preview_image(path: Path) -> np.ndarray:
    """A 0..1 view of a FITS frame, remembered until the file changes."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    hit = _PREVIEW_CACHE.get(key)
    if hit is not None:
        return hit
    data, _header = load_image(str(path))
    image = display_view(data)
    if len(_PREVIEW_CACHE) > 32:
        _PREVIEW_CACHE.clear()
    _PREVIEW_CACHE[key] = image
    return image

