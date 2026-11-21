"""Per-frame numbers while a sequence runs, so a bad night shows itself while it is happening.

After every frame :class:`FrameWatcher` measures, on the frame averaged 2x2 (so a colour mosaic does not
matter): the sky level, the star width, the drift since the first frame, and how bright a set of the
first frame's brightest stars is now compared with then.  It follows each star from frame to frame, so
it needs no WCS.

Reading the numbers:

* every star fainter (or brighter) together, and the sky moving with it, points to cloud, haze or dew;
* the star width or the stars' positions changing points to focus, seeing or tracking;
* one star changing alone is that star (a variable, a neighbour, a bad aperture).

The thresholds here are guesses from one night.  The watcher never raises: a frame it cannot measure
gives a row that says so.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
from photutils.aperture import ApertureStats, CircularAperture

from core.setup_checks import _HALF_BOX, _fit_sigma, detect_any_width
from tools.astrometry import estimate_translation
from tools.imaging import average_2x2, detect_stars, load_image, subtract_background

log = logging.getLogger(__name__)

STATS_NAME = "frame_stats.json"
FIGURE_NAME = "frame_stats.png"
MIN_FRAMES_FOR_WARNING = 3  # a change needs something to be a change from


@dataclass
class FrameWatcher:
    """Follow the brightest stars of the first frame through a sequence.  One instance per sequence."""

    n_stars: int = 12  # the brightest stars of the first frame that are followed
    warn_fraction: float = 0.20  # flux change, against the frames so far, that earns a warning
    drift_warn_fraction: float = 0.30  # change against frame 1 that earns a warning, once, when it is crossed
    width_warn_fraction: float = 0.30
    track_radius_px: float = 8.0  # how far a star may have moved between two frames and still be the same star
    rows: list[dict[str, Any]] = field(default_factory=list)
    _kernel: float = 3.0
    _ref_flux: np.ndarray | None = None
    _ref_xy: np.ndarray | None = None
    _xy: np.ndarray | None = None  # where each followed star was in the last frame
    _radius: float = 5.0
    _sky0: float = 1.0

    # ------------------------------------------------------------------ #
    def update(self, index: int, path: str) -> dict[str, Any]:
        """Measure one frame (``index`` 0 starts a new sequence) and return its row.  Never raises."""
        started = time.monotonic()
        if index == 0:
            self.rows, self._ref_flux, self._ref_xy, self._xy = [], None, None, None
        row: dict[str, Any] = {"index": int(index), "frame": Path(path).name}
        try:
            self._measure(index, path, row)
        except Exception as exc:  # noqa: BLE001 - a watcher that stops the exposures would be worse than none
            row["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("Frame watch: frame %d could not be measured: %s", index + 1, exc)
        row["took_s"] = round(time.monotonic() - started, 1)
        row["warnings"] = self._warnings(row) if "error" not in row else []
        self.rows.append(row)
        return row

    def _measure(self, index: int, path: str, row: dict[str, Any]) -> None:
        raw, _header = load_image(path)
        image = average_2x2(raw)
        sub, rms, sky = subtract_background(image)
        row["sky_adu"] = float(sky)
        if self._ref_xy is None:
            xy, flux, self._kernel = detect_any_width(sub, rms, 5.0)
            ny, nx = sub.shape
            inside = (xy[:, 0] > 30) & (xy[:, 0] < nx - 30) & (xy[:, 1] > 30) & (xy[:, 1] < ny - 30) if len(xy) else []
            xy, flux = (xy[inside], flux[inside]) if len(xy) else (xy, flux)
            row["n_stars"] = int(len(xy))
            if len(xy) < 6:
                raise ValueError(f"only {len(xy)} star(s) in the first frame")
            order = np.argsort(flux)[::-1][: self.n_stars]
            self._ref_xy = self._xy = xy[order].astype(float)
            self._sky0 = float(sky)
            width = self._width(image, self._ref_xy)
            # An aperture that holds the star with room to spare, fixed for the sequence.
            self._radius = float(np.clip(2.5 * (width or 3.0), 4.0, 14.0))
            self._ref_flux = self._flux(sub, self._ref_xy)
            row.update(flux_rel=1.0, flux_spread=0.0, sky_rel=1.0, fwhm_px=width, dx_px=0.0, dy_px=0.0, n_followed=len(order))
            return
        xy, _flux = detect_stars(sub, rms, self._kernel, 5.0)
        row["n_stars"] = int(len(xy))
        assert self._xy is not None and self._ref_flux is not None and self._ref_xy is not None
        shift = estimate_translation(self._xy, xy, max_shift=120.0, tol=4.0, min_matches=5) if len(xy) >= 5 else None
        moved = self._xy + ([shift["dx"], shift["dy"]] if shift else [0.0, 0.0])
        found = np.full(len(moved), np.nan)
        updated = self._xy.copy()
        if len(xy):
            for k, guess in enumerate(moved):
                d = np.hypot(xy[:, 0] - guess[0], xy[:, 1] - guess[1])
                j = int(np.argmin(d))
                if d[j] <= self.track_radius_px:
                    updated[k], found[k] = xy[j], 1.0
        seen = np.isfinite(found)
        row["n_followed"] = int(seen.sum())
        if seen.sum() < 3:
            raise ValueError(f"only {int(seen.sum())} of the followed stars were found")
        flux = self._flux(sub, updated)
        ratio = flux / self._ref_flux
        good = seen & np.isfinite(ratio) & (self._ref_flux > 0)
        median = float(np.median(ratio[good]))
        row["flux_rel"] = median
        row["flux_spread"] = float(1.4826 * np.median(np.abs(ratio[good] - median))) if good.sum() > 2 else None
        row["sky_rel"] = float(sky / self._sky0) if self._sky0 else None
        row["fwhm_px"] = self._width(image, updated[seen])
        offset = updated[seen] - self._ref_xy[seen]
        row["dx_px"], row["dy_px"] = float(np.median(offset[:, 0])), float(np.median(offset[:, 1]))
        self._xy = np.where(seen[:, None], updated, moved)

    def _flux(self, sub: np.ndarray, xy: np.ndarray) -> np.ndarray:
        stats = ApertureStats(sub, CircularAperture(xy, r=self._radius))
        return np.asarray(stats.sum, dtype=float)

    @staticmethod
    def _width(image: np.ndarray, xy: np.ndarray) -> float | None:
        """Median FWHM (px) of the followed stars on the 2x2-averaged frame (a little wider than the raw one)."""
        ny, nx = image.shape
        sigmas = []
        for x, y in np.round(xy).astype(int):
            if _HALF_BOX <= x < nx - _HALF_BOX and _HALF_BOX <= y < ny - _HALF_BOX:
                cut = image[y - _HALF_BOX : y + _HALF_BOX + 1, x - _HALF_BOX : x + _HALF_BOX + 1].astype(float)
                sigma = _fit_sigma(cut - np.median(cut))
                if sigma is not None:
                    sigmas.append(sigma)
        return float(2.3548 * np.median(sigmas)) if len(sigmas) >= 3 else None

    # ------------------------------------------------------------------ #
    def _warnings(self, row: dict[str, Any]) -> list[str]:
        out: list[str] = []
        before = [r for r in self.rows if r.get("flux_rel") is not None]
        if len(before) < MIN_FRAMES_FOR_WARNING or row.get("flux_rel") is None:
            return out
        base = float(np.median([r["flux_rel"] for r in before[-8:]]))
        change = row["flux_rel"] / base - 1.0 if base else 0.0
        if abs(change) > self.warn_fraction:
            text = f"the stars are {abs(change):.0%} {'fainter' if change < 0 else 'brighter'} than in the frames so far"
            spread = row.get("flux_spread")
            if spread is not None and spread < 0.5 * abs(change):
                text += " and they changed together (cloud, haze or dew would do this)"
            elif spread is not None:
                text += f" but unevenly (spread {spread:.0%}: look at tracking or the individual stars)"
            sky = row.get("sky_rel")
            if sky is not None and abs(sky - 1.0) > 0.05:
                text += f"; the sky is {sky - 1.0:+.0%} against the first frame"
            out.append("transparency: " + text)
        elif abs(row["flux_rel"] - 1.0) > self.drift_warn_fraction and abs(before[-1]["flux_rel"] - 1.0) <= self.drift_warn_fraction:
            # A slow fade never jumps against the last few frames, so it also has to be caught against frame 1.
            out.append(
                f"transparency: the stars have {'faded to' if row['flux_rel'] < 1 else 'risen to'} {row['flux_rel']:.2f} of frame 1"
                + (f" and the sky is {row['sky_rel'] - 1.0:+.0%}" if row.get("sky_rel") is not None else "")
                + " (a slow change like this is cloud, haze or dew thickening, not a single bad frame)"
            )
        widths = [r["fwhm_px"] for r in before[-8:] if r.get("fwhm_px")]
        if row.get("fwhm_px") and len(widths) >= MIN_FRAMES_FOR_WARNING:
            wbase = float(np.median(widths))
            if abs(row["fwhm_px"] / wbase - 1.0) > self.width_warn_fraction:
                out.append(f"star width: {row['fwhm_px']:.1f} px against {wbase:.1f} px in the frames so far (focus, seeing or tracking)")
        return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_row(row: dict[str, Any], total: int | None = None) -> str:
    """One line for the console and the mission log."""
    of = f"/{total}" if total else ""
    head = f"Frame {row['index'] + 1}{of}"
    if "error" in row:
        return f"{head}: not measured ({row['error']})"
    if row["index"] == 0:
        return (f"{head}: reference, {row.get('n_followed')} stars followed, FWHM {_n(row.get('fwhm_px'), '.1f')} px, "
                f"sky {row['sky_adu']:.0f} ADU [{row['took_s']} s]")
    spread = row.get("flux_spread")
    return (
        f"{head}: flux {_n(row.get('flux_rel'), '.2f')} of frame 1"
        + (f" (spread {spread:.0%})" if spread is not None else "")
        + f", sky {_n(row.get('sky_rel'), '.2f')}, FWHM {_n(row.get('fwhm_px'), '.1f')} px, "
        f"drift ({row['dx_px']:+.0f}, {row['dy_px']:+.0f}) px, {row.get('n_followed')} stars followed [{row['took_s']} s]"
    )


def _n(value: float | None, spec: str) -> str:
    return "?" if value is None else format(value, spec)


def save_stats(rows: Sequence[dict[str, Any]], mission_dir: Path) -> Path:
    """``frame_stats.json`` and ``frame_stats.png`` in the mission folder."""
    mission_dir.mkdir(parents=True, exist_ok=True)
    path = mission_dir / STATS_NAME
    tmp = mission_dir / f".{STATS_NAME}.tmp"  # whole file or none: the dashboard reads this while the sequence runs
    tmp.write_text(json.dumps(list(rows), indent=2, default=str))
    tmp.replace(path)
    try:
        save_figure(rows, mission_dir / FIGURE_NAME)
    except Exception as exc:  # noqa: BLE001 - the numbers are saved; the picture is a convenience
        log.warning("Frame watch: could not draw the figure: %s", exc)
    return path


def save_figure(rows: Sequence[dict[str, Any]], dest: Path) -> None:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    ok = [r for r in rows if "error" not in r and r.get("flux_rel") is not None]
    fig = Figure(figsize=(7.0, 6.4), dpi=100)
    FigureCanvasAgg(fig)
    axes = fig.subplots(4, 1, sharex=True)
    n = [r["index"] + 1 for r in ok]

    def series(key: str) -> list[float]:
        return [np.nan if r.get(key) is None else r[key] for r in ok]

    flux, spread = np.array(series("flux_rel")), np.array(series("flux_spread"))
    axes[0].errorbar(n, flux, yerr=np.nan_to_num(spread), fmt="o-", ms=3, lw=1, color="tab:blue")
    axes[0].axhline(1.0, color="0.7", lw=0.8)
    axes[0].set_ylabel("star flux\n(frame 1 = 1)")
    axes[1].plot(n, series("sky_rel"), "o-", ms=3, lw=1, color="tab:orange")
    axes[1].axhline(1.0, color="0.7", lw=0.8)
    axes[1].set_ylabel("sky level\n(frame 1 = 1)")
    axes[2].plot(n, series("fwhm_px"), "o-", ms=3, lw=1, color="tab:green")
    axes[2].set_ylabel("star width\n(FWHM, px)")
    axes[3].plot(n, series("dx_px"), "o-", ms=3, lw=1, label="x")
    axes[3].plot(n, series("dy_px"), "o-", ms=3, lw=1, label="y")
    axes[3].set_ylabel("drift (px)")
    axes[3].set_xlabel("frame")
    axes[3].legend(loc="best", fontsize=8)
    for ax in axes:
        ax.grid(alpha=0.25)
    fig.suptitle("Per-frame watch: bright stars of frame 1 followed through the sequence", fontsize=10)
    fig.tight_layout()
    tmp = dest.with_name(f".{dest.stem}.tmp{dest.suffix}")
    fig.savefig(str(tmp))
    tmp.replace(dest)


def watch_sequence(paths: Sequence[str], progress: Callable[[dict[str, Any]], None] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    """Run a watcher over frames already on disk (``--reanalyze``)."""
    watcher = FrameWatcher(**kwargs)
    for i, path in enumerate(paths):
        row = watcher.update(i, str(path))
        if progress:
            progress(row)
    return watcher.rows


def summary(rows: Sequence[dict[str, Any]]) -> str:
    """Two sentences on how steady the sequence was."""
    ok = [r for r in rows if "error" not in r and r.get("flux_rel") is not None]
    if len(ok) < 3:
        return f"Only {len(ok)} frame(s) measured; no steadiness figure."
    flux = np.array([r["flux_rel"] for r in ok])
    warned = sum(1 for r in rows if r.get("warnings"))
    widths = [r["fwhm_px"] for r in ok if r.get("fwhm_px")]
    text = (
        f"Star flux ranged {flux.min():.2f} to {flux.max():.2f} of frame 1 (scatter {np.std(flux):.0%}); "
        f"{warned} of {len(rows)} frames carried a warning."
    )
    if widths:
        text += f"  Star width {min(widths):.1f} to {max(widths):.1f} px."
    return text
