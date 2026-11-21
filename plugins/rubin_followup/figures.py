"""The follow-up figure: the stacked field around the alert position, and Rubin's light curve with ours."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Circle

BAND_COLOURS = {"u": "#7b3294", "g": "#2ca25f", "r": "#d7301f", "i": "#e08214", "z": "#8c510a", "y": "#636363", "?": "#999999"}
CUTOUT_HALF_PX = 40


def _days(iso: str, ref: datetime) -> float:
    t = datetime.fromisoformat(iso)
    return (t - ref).total_seconds() / 86400.0


def follow_up_figure(
    out_path: str | Path, stack: np.ndarray, xy: tuple[float, float], name: str, light_curve: dict, expected: dict | None,
    classification: str, params,
) -> str:
    fig = Figure(figsize=(9.5, 3.8))
    FigureCanvasAgg(fig)
    ax_img, ax_lc = fig.subplots(1, 2, gridspec_kw={"width_ratios": [1, 1.5]})

    # -- finder cutout ------------------------------------------------------ #
    x, y = xy
    ny, nx = stack.shape
    xi, yi = int(round(x)), int(round(y))
    x0, x1 = max(xi - CUTOUT_HALF_PX, 0), min(xi + CUTOUT_HALF_PX + 1, nx)
    y0, y1 = max(yi - CUTOUT_HALF_PX, 0), min(yi + CUTOUT_HALF_PX + 1, ny)
    if x1 > x0 and y1 > y0:
        cut = stack[y0:y1, x0:x1]
        lo, hi = np.percentile(cut, [5.0, 99.7])
        ax_img.imshow(np.arcsinh((cut - lo) / max(hi - lo, 1e-6) * 10.0), origin="lower", cmap="gray_r",
                      extent=(x0 - 0.5, x1 - 0.5, y0 - 0.5, y1 - 0.5))
        ax_img.add_patch(Circle((x, y), params.aperture_radius_px * 2.2, fill=False, ec="tab:red", lw=1.2))
        ax_img.set_xlim(x0 - 0.5, x1 - 0.5)
        ax_img.set_ylim(y0 - 0.5, y1 - 0.5)
    else:
        ax_img.text(0.5, 0.5, "alert position\noutside the stack", ha="center", va="center", transform=ax_img.transAxes)
    ax_img.set_title(f"{name}: stack (alert position circled)", fontsize=9)
    ax_img.set_xticks([])
    ax_img.set_yticks([])

    # -- light curve -------------------------------------------------------- #
    ours = light_curve.get("ours") or {}
    ref = datetime.fromisoformat(ours["utc_iso"]) if ours.get("utc_iso") else None
    pts = light_curve.get("rubin") or []
    if ref is not None:
        for band in sorted({p["band"] for p in pts}):
            sel = [p for p in pts if p["band"] == band]
            ax_lc.errorbar([_days(p["utc_iso"], ref) for p in sel], [p["mag"] for p in sel],
                           yerr=[p["mag_err"] or 0.0 for p in sel], fmt="o", ms=4, lw=0.8,
                           color=BAND_COLOURS.get(band, "#999999"), label=f"Rubin {band}")
        if expected and expected.get("expected_mag") is not None:
            ax_lc.plot([0.0], [expected["expected_mag"]], marker="D", mfc="none", mec="tab:blue", ms=7, ls="none", label="Rubin-implied")
        if ours.get("detected") and ours.get("mag") is not None:
            ax_lc.errorbar([0.0], [ours["mag"]], yerr=[ours.get("mag_err") or 0.0], fmt="*", ms=13, color="black", label="Seestar (G-like)")
        elif ours.get("limit_mag_5sigma") is not None:
            ax_lc.plot([0.0], [ours["limit_mag_5sigma"]], marker="v", ms=10, color="black", ls="none", label="Seestar 5-sigma limit")
    ax_lc.invert_yaxis()
    ax_lc.set_xlabel("days relative to the Seestar stack")
    ax_lc.set_ylabel("magnitude")
    ax_lc.set_title(classification.replace("_", " "), fontsize=9)
    ax_lc.grid(alpha=0.25)
    if ax_lc.get_legend_handles_labels()[0]:
        ax_lc.legend(fontsize=7, loc="best")

    fig.tight_layout()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    return str(out)
