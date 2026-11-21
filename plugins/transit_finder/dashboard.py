"""Dashboard view for the transit finder: the light curve, the transit fit and the catalogue comparison."""

from __future__ import annotations

from datetime import datetime

import plotly.graph_objects as go

from dashboard.plugin_ui import Summary, ViewContext, figure_json
from dashboard.vocab import GLOSSARY, format_distance, plural

LABEL = "Exoplanet transit"


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def distance_text(distance: dict | None) -> str | None:
    """``"153 pc (500 ly)"`` for the host star, from the archive value (or the Gaia parallax if that is all there is)."""
    d = distance or {}
    return format_distance(d.get("archive_pc") or d.get("gaia_pc"))


def distance_detail(distance: dict | None) -> str | None:
    """The sentence under the title: where the number comes from, and whether Gaia agrees."""
    d = distance or {}
    if not distance_text(d):
        return None
    parts = []
    if d.get("archive_pc"):
        parts.append(f"{d['archive_pc']:.0f} pc in the NASA Exoplanet Archive")
    if d.get("gaia_pc"):
        parts.append(
            f"{d['gaia_pc']:.0f} pc from the Gaia parallax of the measured star "
            f"({d['gaia_parallax_mas']:.2f} ± {d['gaia_parallax_err_mas']:.2f} mas)"
        )
    text = "Distance to the host star: " + "; ".join(parts) + "."
    if d.get("consistent") is False:
        text += " These disagree."
    return text


def _hours_from(t0_iso: str, other_iso: str) -> float:
    return (_parse(other_iso) - _parse(t0_iso)).total_seconds() / 3600.0


def light_curve_figure(findings: dict, crossref: dict) -> go.Figure | None:
    lc = findings.get("light_curve") or {}
    if not lc.get("t_s"):
        return None
    t = [s / 3600.0 for s in lc["t_s"]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=t, y=lc["flux"], mode="markers", name="differential flux",
        marker=dict(size=5, color="#6c757d"),
        error_y=dict(type="data", array=lc.get("err"), visible=bool(lc.get("err")), thickness=0.8, color="#adb5bd"),
    ))
    if lc.get("baseline"):
        fig.add_trace(go.Scatter(x=t, y=lc["baseline"], mode="lines", name="baseline",
                                 line=dict(color="#0d6efd", dash="dot", width=1.5)))
    if lc.get("model"):
        fig.add_trace(go.Scatter(x=t, y=lc["model"], mode="lines", name="transit fit",
                                 line=dict(color="#dc3545", width=2)))

    expected = (crossref or {}).get("expected") or {}
    if expected.get("t_mid_iso") and lc.get("t0_iso"):
        mid = _hours_from(lc["t0_iso"], expected["t_mid_iso"])
        half = (expected.get("duration_h") or 0.0) / 2.0
        if half:
            fig.add_vrect(x0=mid - half, x1=mid + half, fillcolor="#198754", opacity=0.12, line_width=0)
        fig.add_vline(x=mid, line=dict(color="#198754", dash="dash", width=1))
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name="catalogue transit",
                                 line=dict(color="#198754", dash="dash")))
    fig.update_layout(
        height=380, margin=dict(l=10, r=10, t=30, b=10), xaxis_title=f"hours since {lc.get('t0_iso', '')[:19]} UTC",
        yaxis_title="relative flux", legend=dict(orientation="h", y=1.12),
    )
    return fig


def _outcome(obs: dict) -> str:
    """One classification per observed field: the cross-reference of its detection, or why there was none."""
    xref = obs.get("crossref") or {}
    entries = xref.get("entries") or []
    if entries:
        return str(entries[0].get("classification") or "unverified")
    nd = xref.get("non_detection") or {}
    if nd.get("classification"):
        return str(nd["classification"])
    if nd.get("expected") is False:
        return "control_clean"
    return "not_detected" if nd else "unverified"


#: outcome -> (phrase counted in the "found" sentence, is it worth a human look?)
_OUTCOMES = {
    "known_transit_confirmed": ("known transit confirmed", False),
    "control_clean": ("control field clean", False),
    "known_transit_timing_offset": ("transit seen but off the predicted time", True),
    "known_transit_shape_mismatch": ("transit seen but a different shape from the catalogue", True),
    "unexpected_dip": ("dip where no transit was expected", True),
    "target_identity_mismatch": ("possibly the wrong star measured", True),
    "expected_transit_not_detected": ("expected transit not seen", True),
    "expected_window_not_covered": ("expected transit window missed by the data", False),
}
#: In order of how much a human should care; the first one present decides the "meaning" sentence.
_MEANINGS = (
    ("target_identity_mismatch", "attention",
     "The star that was measured may not be the target, so do not trust these numbers until that is resolved."),
    ("unexpected_dip", "attention",
     "A dip with no predicted transit is not evidence of a new planet: eclipsing binaries, blended stars, clouds and "
     "instrument effects all cause dips."),
    ("expected_transit_not_detected", "attention",
     "The catalogue says a transit should be here and the data do not show one. The schedule may be out of date or the "
     "data too noisy."),
    ("known_transit_timing_offset", "attention",
     "The transit happened, but not when the catalogue predicts. Schedules drift, so this is worth reporting rather "
     "than an error."),
    ("known_transit_shape_mismatch", "attention",
     "The timing agrees, but the depth or duration differ from the catalogue by more than the tolerance."),
)


def _catalogue_rows(det: dict, xref: dict) -> list[dict]:
    expected = xref.get("expected")
    entry = next(iter(xref.get("entries") or []), {})
    if not expected:
        return []

    def fmt(v: object) -> str:
        return "-" if v is None else (f"{v:.2f}" if isinstance(v, (int, float)) else str(v))

    return [
        {"quantity": "mid-time offset (min)", "measured": fmt(entry.get("t_mid_residual_min")), "catalogue": "0 (ephemeris)"},
        {"quantity": "depth (%)", "measured": fmt(det.get("depth_pct")), "catalogue": fmt(expected.get("depth_pct"))},
        {"quantity": "duration (h)", "measured": fmt(det.get("duration_h")), "catalogue": fmt(expected.get("duration_h"))},
    ]


def evidence(ctx: ViewContext) -> dict:
    """Light curves and fit numbers as JSON."""
    n_obs = len(ctx.observations)
    entries = [(o, e) for o in ctx.observations for e in ((o.get("crossref") or {}).get("entries") or [])]
    confirmed = sum(e.get("classification") == "known_transit_confirmed" for _, e in entries)
    items = []
    for obs in ctx.observations:
        findings, xref = obs.get("findings") or {}, obs.get("crossref") or {}
        fit = findings.get("fit") or {}
        plan = obs.get("exposure_plan") or {}
        n_frames = len(obs.get("fits_paths") or [])
        captions = []
        detail = distance_detail(xref.get("distance"))
        if detail:
            captions.append(detail)
        if xref.get("expected"):
            exp = xref["expected"]
            captions.append(
                f"Expected transit of {exp.get('planet')} at {str(exp.get('t_mid_iso'))[:16]} UTC "
                f"(± {exp.get('t_mid_err_min', '?')} min)."
            )
        else:
            captions.append("Control field: no transit expected.")
        captions.append(
            f"{n_frames}/{plan.get('count', '?')} frames, {plan.get('exposure_s', '?')} s exposures, "
            f"{findings.get('n_comps', '?')} comparison stars."
        )
        if xref.get("summary"):
            captions.append(xref["summary"])
        ident = xref.get("identity") or {}
        if ident.get("status"):
            captions.append(f"Target identity (Gaia): {ident['status']}")
        captions.extend(xref.get("notes") or [])
        metrics = []
        if not fit.get("ok"):
            metrics.append({"label": "Transit fit", "value": "failed"})
            if fit.get("reason"):
                captions.append(fit["reason"])
        elif fit:
            metrics.append({"label": "Transit detected", "value": "yes" if fit.get("significant") else "no"})
            if fit.get("depth_pct") is not None:
                metrics.append({"label": "Dip depth" if ctx.simple else "Depth", "value": f"{fit['depth_pct']:.2f} ± {fit['depth_err_pct']:.2f} %", "help": GLOSSARY["depth"]})
            if fit.get("duration_h") is not None:
                metrics.append({"label": "Duration", "value": f"{fit['duration_h']:.2f} h", "help": GLOSSARY["duration"]})
            if fit.get("snr") is not None:
                metrics.append({"label": "Signal strength (SNR)" if ctx.simple else "Depth SNR", "value": f"{fit['snr']:.1f}", "help": GLOSSARY["SNR"]})
            if not ctx.simple and fit.get("delta_bic") is not None:
                metrics.append({"label": "ΔBIC vs no transit", "value": f"{fit['delta_bic']:.0f}", "help": GLOSSARY["delta_bic"]})
        dets = findings.get("detections") or []
        tables = []
        rows = _catalogue_rows(dets[0], xref) if dets else []
        if rows:
            tables.append({"title": "Compared with the catalogue", "rows": rows})
        fig = figure_json(light_curve_figure(findings, xref))
        items.append({
            "title": (obs.get("target_name") or "Field") + (f" · {distance_text(xref.get('distance'))}" if distance_text(xref.get("distance")) else ""),
            "summary": findings.get("summary"),
            "captions": captions,
            "metrics": metrics,
            "plotly": [{"title": "Light curve", "figure": fig}] if fig else [],
            "tables": tables,
            "warnings": findings.get("warnings") or [],
        })
    if not ctx.observations:
        return {"captions": ["Waiting for the first field to be observed and reduced. Transit runs take many frames per field."]}
    return {
        "metrics": [
            {"label": "Fields observed", "value": n_obs, "help": "Stars (or comparison fields) imaged so far."},
            {"label": "Transits detected", "value": sum(len((o.get("findings") or {}).get("detections", [])) for o in ctx.observations)},
            {"label": "Confirmed vs catalogue", "value": confirmed, "help": "Detections whose timing, depth and duration agree with the catalogue."},
        ],
        "observations": items,
    }


def summarise(ctx: ViewContext) -> Summary:
    outcomes = [_outcome(o) for o in ctx.observations]
    counts: dict[str, int] = {}
    for kind in outcomes:
        counts[kind] = counts.get(kind, 0) + 1
    parts = [f"{n} {_OUTCOMES.get(kind, (kind.replace('_', ' '), False))[0]}" for kind, n in counts.items()]
    found = f"{plural(len(outcomes), 'field')} analysed: " + "; ".join(parts) + "."
    for kind, verdict, meaning in _MEANINGS:
        if kind in counts:
            return Summary(found, meaning, verdict)
    good = counts.get("known_transit_confirmed", 0) + counts.get("control_clean", 0)
    if good == len(outcomes):
        return Summary(found, "The measurements agree with the catalogue: the pipeline recovers a known planet and stays quiet on control fields.", "good")
    return Summary(found, "Nothing conclusive yet. The Results tab shows each field.", "neutral")

