"""The operator's view of the first frame, beside the focus and pointing result.

Written by ``core.setup_checks`` the moment that frame is on disk, before the rest of the
sequence.  The dashboard only reads it.  The checks still decide a hard stop; the picture
is so a person can see a tree, a roof, dew or cloud, which star measurements miss.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.setup_checks import PREVIEW_NAME
from core.state import TERMINAL

SAVED_FRAME = (
    "This is the saved frame, stretched so a tree, a roof, dew or cloud can be seen. "
    "The focus and pointing checks decide whether the sequence stops. "
    "Orientation against the Seestar app is unconfirmed."
)
PENDING = (
    "The first frame will appear here, beside the focus and pointing result, once it is on disk. "
    "The rest of the sequence waits for that frame and for the checks."
)
MEASURING = (
    "The first frame is on disk. Focus and pointing are still being measured, "
    "and the rest of the sequence waits for them. "
    "Orientation against the Seestar app is unconfirmed."
)
CONTINUES = (
    "The checks let the sequence continue. If the picture shows a tree, a roof, dew or cloud, "
    "stop the run with `python main.py --stop`."
)
STOPPED = "A check failed, so the rest of the sequence is not taken."
NO_PICTURE = "The picture could not be written. The checks still decide whether the sequence stops."


@dataclass(frozen=True)
class FirstFrame:
    """What Overview can show: a report, a picture, or the picture while the checks are still running."""

    report: dict | None
    preview: Path | None


def will_show_first_frame(mission: dict) -> bool:
    """A real telescope run always measures the first frame. Archive replays and the mock do not."""
    if mission.get("data_source") == "archive":
        return False
    return (mission.get("config") or {}).get("mock_mode") is False


def locate_first_frame(data_dir: Path, mission_id: str) -> FirstFrame | None:
    """The view and the report in the mission folder, or None when neither has been written."""
    mission_dir = Path(data_dir) / "missions" / mission_id
    report = _read_report(mission_dir / "setup_check.json")
    name = (report or {}).get("preview") or PREVIEW_NAME
    preview = mission_dir / name
    if not preview.is_file():
        preview_path = None
    else:
        preview_path = preview
    if report is None and preview_path is None:
        return None
    return FirstFrame(report, preview_path)


def show_above_sections(mission: dict, found: FirstFrame | None) -> bool:
    """Keep the picture in sight for the whole live run, whichever section is open.

    Once the mission has ended the same picture stays on Overview, where the setup record lives.
    """
    status = mission.get("status")
    if found is not None and status not in TERMINAL:
        return True
    return found is None and status == "OBSERVING" and will_show_first_frame(mission)


LIVE_CAPTION = (
    "The newest saved frame, stretched so cloud, dew or a tree can be seen (a view, not a measurement), beside how the "
    "bright stars of frame 1 are doing. All stars fading together, with the sky rising, is cloud, haze or dew; the star "
    "width or position changing is focus or tracking."
)


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return [r for r in rows if isinstance(r, dict) and "index" in r] if isinstance(rows, list) else []


def _read_report(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return report if isinstance(report, dict) else None

