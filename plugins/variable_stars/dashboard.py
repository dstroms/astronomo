"""Dashboard view for variable-star photometry: the night's magnitude and, when fitted, the time of minimum."""

from __future__ import annotations

import plotly.graph_objects as go

from dashboard.plugin_ui import Summary, ViewContext, figure_json

LABEL = "Variable star"


def _measurement(obs: dict) -> dict:
    return (obs.get("crossref") or {}).get("measurement") or {}


def light_curve_figure(findings: dict, crossref: dict) -> go.Figure | None:
    meas = (crossref or {}).get("measurement") or {}
    series = meas.get("series_mag") or []
    lc = (findings or {}).get("light_curve") or {}
    if not series or not lc.get("t_s") or len(series) != len(lc["t_s"]):
        return None
    hours = [s / 3600.0 for s in lc["t_s"]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=hours, y=series, mode="markers", name="magnitude", marker=dict(size=6, color="#6c757d")))
    eclipse = meas.get("eclipse") or {}
    if eclipse.get("t_mid_s") is not None:
        fig.add_vline(x=float(eclipse["t_mid_s"]) / 3600.0, line=dict(color="#dc3545", dash="dash", width=1))
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=30, b=10),
        xaxis_title=f"hours since {(lc.get('t0_iso') or '')[:19]} UTC",
        yaxis_title="magnitude (Gaia G zero point)", yaxis=dict(autorange="reversed"),
        legend=dict(orientation="h", y=1.12),
    )
    return fig


def evidence(ctx: ViewContext) -> dict:
    """Night magnitudes and the light curve as JSON."""
    if not ctx.observations:
        return {"captions": ["No star has been measured yet."]}
    items = []
    for obs in ctx.observations:
        meas = _measurement(obs)
        metrics = []
        captions = []
        if meas.get("mag") is None:
            metrics.append({"label": "Magnitude", "value": "not measured"})
            captions.append(meas.get("reason") or (obs.get("findings") or {}).get("summary") or "")
        else:
            metrics.append({"label": "Magnitude", "value": f"{meas['mag']:.3f} ± {meas.get('mag_err', float('nan')):.3f}"})
            metrics.append({"label": "What it is", "value": str(meas.get("classification") or "").replace("_", " ") or "—"})
            eclipse = meas.get("eclipse") or {}
            metrics.append({"label": "Minimum (UTC)", "value": (eclipse.get("t_min_iso") or "none")[:16].replace("T", " ")})
        note = (obs.get("target") or {}).get("window_note")
        if note:
            captions.append(note)
        fig = figure_json(light_curve_figure(obs.get("findings") or {}, obs.get("crossref") or {}))
        items.append({
            "title": obs.get("target_name") or "Star",
            "metrics": metrics,
            "captions": [c for c in captions if c],
            "plotly": [{"title": "Light curve", "figure": fig}] if fig else [],
        })
    return {"observations": items}


def summarise(ctx: ViewContext) -> Summary | None:
    if not ctx.observations:
        return None
    names = []
    attention = False
    for obs in ctx.observations:
        meas = _measurement(obs)
        if meas.get("mag") is None:
            continue
        names.append(f"{obs.get('target_name')} {meas['mag']:.2f}")
        if meas.get("classification") in ("eclipse_offset", "brighter_than_range", "identity_mismatch"):
            attention = True
    if not names:
        return Summary(found="No magnitude was measured.", meaning="There is nothing to send.", verdict="neutral")
    found = "Measured " + ", ".join(names) + "."
    if attention:
        return Summary(
            found=found,
            meaning="One result is outside what was expected. It is a lead, not a discovery, until someone checks it.",
            verdict="attention",
        )
    return Summary(
        found=found,
        meaning="A magnitude for the record. It is not a transformed V magnitude, and nothing here is a discovery.",
        verdict="good",
    )

