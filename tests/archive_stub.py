"""A tiny archive-capable plugin for harness tests: no astronomy, just visits on a schedule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from astropy.io import fits

from core.archive import FrameBatch
from core.base_plugin import BaseExperimentPlugin

T0 = datetime(2019, 1, 20, 7, 0, 0, tzinfo=timezone.utc)
#: Visit start offsets: a burst, a revisit two hours later, then one three days on.
DEFAULT_OFFSETS = (timedelta(0), timedelta(hours=2), timedelta(days=3))


class StubSource:
    name = "stub-survey"
    credit = "Stub survey (tests)"

    def __init__(self, frames_per_visit: int = 3, cadence_s: float = 300.0):
        self.frames_per_visit = frames_per_visit
        self.cadence_s = cadence_s
        self.acquired: list[str] = []

    def acquire(self, target, out_dir: Path, on_frame=None) -> FrameBatch:
        out_dir.mkdir(parents=True, exist_ok=True)
        start = datetime.fromisoformat(target["not_before"])
        rng = np.random.default_rng(1)
        paths = []
        for i in range(self.frames_per_visit):
            t = start + timedelta(seconds=i * self.cadence_s)
            if on_frame:
                on_frame(i, self.frames_per_visit, t)
            hdr = fits.Header()
            hdr["DATE-OBS"] = t.strftime("%Y-%m-%dT%H:%M:%S")
            hdr["EXPTIME"] = 30.0
            path = out_dir / f"f{i:02d}.fits"
            fits.PrimaryHDU(rng.normal(100, 5, (32, 32)).astype("float32"), hdr).writeto(path, overwrite=True)
            paths.append(str(path))
        self.acquired.append(target["name"])
        return FrameBatch(paths, {"gain": 0, "exposure_s": 30.0, "filter": "r"}, {"source": self.name, "credit": self.credit})


class StubArchivePlugin(BaseExperimentPlugin):
    name = "stub_archive"
    description = "test double"
    data_sources = ("telescope", "archive")

    def __init__(self, settings, offsets=DEFAULT_OFFSETS, source: StubSource | None = None, scheduled_telescope: bool = False):
        super().__init__(settings)
        self.offsets = offsets
        self.source = source or StubSource()
        self.scheduled_telescope = scheduled_telescope

    def archive_source(self):
        return self.source

    def target_limit(self) -> int:
        return 100

    def generate_targets(self):
        out = []
        for i, off in enumerate(self.offsets):
            t = {"name": f"visit-{i + 1}", "ra_deg": 10.0 + i, "dec_deg": 5.0, "priority": 1.0}
            if self.settings.archive_mode or self.scheduled_telescope:
                t["not_before"] = (T0 + off).isoformat()
            out.append(t)
        return out

    def get_exposure_plan(self):
        return {"gain": 80, "exposure_s": 10.0, "filter": "IRCUT", "count": 2}

    def analyze_frames(self, fits_paths):
        return {"summary": f"{len(fits_paths)} frames", "detections": [], "figures": []}
