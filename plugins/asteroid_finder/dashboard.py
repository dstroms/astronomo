"""Dashboard view for the asteroid finder: candidate movers, their tracks and what they matched."""

from __future__ import annotations

import plotly.graph_objects as go

from dashboard.plugin_ui import Summary, ViewContext, figure_json
from dashboard.vocab import GLOSSARY, plural

LABEL = "Asteroid search"


#: Plain wording for the classifications, used in the simple view.
_CLASS_PLAIN = {
    "known_object_match": "known object",
    "unidentified_candidate": "unidentified",
    "position_match_rate_mismatch": "right place, wrong speed",
    "unverified": "not checked",
}


def detection_rows(observations: list[dict], simple: bool = False) -> list[dict]:
    rows = []
    for obs in observations:
        findings = obs.get("findings") or {}
        xref = {e.get("detection_id"): e for e in (obs.get("crossref") or {}).get("entries", [])}
        for det in findings.get("detections", []):
            e = xref.get(det.get("id"), {})
            rows.append(
                {
                    "field": obs.get("target_name"),
                    "id": det.get("id"),
                    "epoch (UTC)": (det.get("epoch_mid_iso") or "")[:19],
                    "RA (deg)": e.get("ra_deg", det.get("ra_deg")),
                    "Dec (deg)": e.get("dec_deg", det.get("dec_deg")),
                    "rate (\"/min)": e.get("rate_arcsec_per_min", det.get("rate_arcsec_per_min")),
                    "points": f"{det.get('n_points')}/{det.get('n_frames')}",
                    "SNR~": det.get("snr_median"),
                    "classification": (
                        _CLASS_PLAIN.get(e.get("classification", "unverified"), str(e.get("classification")).replace("_", " "))
                        if simple else e.get("classification", "unverified")
                    ),
                    "match": (e.get("match") or {}).get("designation"),
                }
            )
    return rows


def _track_figure(detections: list[dict]) -> go.Figure:
    fig = go.Figure()
    for det in detections:
        pts = det.get("points") or []
        if not pts:
            continue
        fig.add_trace(go.Scatter(
            x=[p["x"] for p in pts], y=[p["y"] for p in pts], mode="lines+markers", name=det.get("id", "?"),
            marker=dict(size=[9 if p.get("detected") else 5 for p in pts],
                        symbol=["circle" if p.get("detected") else "circle-open" for p in pts]),
            hovertext=[f"frame {p.get('frame_index', i)}" for i, p in enumerate(pts)],
        ))
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=30, b=10), title="Candidate tracks in the reference frame (px)",
        xaxis_title="x (px)", yaxis=dict(title="y (px)", autorange="reversed", scaleanchor="x"),
    )
    return fig


def _counts(observations: list[dict]) -> tuple[int, int, int, int]:
    """``(fields, candidate tracks, matched a known object, unidentified)``."""
    entries = [e for o in observations for e in (o.get("crossref") or {}).get("entries", [])]
    return (
        len(observations),
        sum(len((o.get("findings") or {}).get("detections", [])) for o in observations),
        sum(e.get("classification") == "known_object_match" for e in entries),
        sum(e.get("classification") == "unidentified_candidate" for e in entries),
    )


def evidence(ctx: ViewContext) -> dict:
    """Candidate tracks and matches as JSON."""
    n_obs, n_tracks, n_known, n_unid = _counts(ctx.observations)
    if not ctx.observations:
        return {"captions": ["Waiting for the first field to be observed and reduced."]}
    items = []
    for obs in ctx.observations:
        findings, xref = obs.get("findings") or {}, obs.get("crossref") or {}
        dets = findings.get("detections") or []
        fig = figure_json(_track_figure(dets)) if dets else None
        captions = []
        if not dets:
            captions.append("No moving-object candidates in this field.")
        if xref.get("summary"):
            captions.append(xref["summary"])
        cal = xref.get("calibration") or {}
        if cal.get("status"):
            captions.append(
                f"Astrometry: {cal['status']}"
                + (f" (rms {cal['rms_arcsec']:.2f} arcsec, {cal.get('n_matches')} Gaia stars)"
                   if cal.get("rms_arcsec") is not None else "")
            )
        items.append({
            "title": obs.get("target_name") or "Field",
            "summary": findings.get("summary"),
            "captions": captions,
            "plotly": [{"title": "Candidate tracks in the reference frame (px)", "figure": fig}] if fig else [],
            "warnings": findings.get("warnings") or [],
        })
    rows = detection_rows(ctx.observations, ctx.simple)
    return {
        "metrics": [
            {"label": "Fields observed", "value": n_obs, "help": "Patches of sky imaged so far."},
            {"label": "Moving-object candidates" if ctx.simple else "Candidate tracks", "value": n_tracks,
             "help": "Things that moved between frames the way an asteroid would."},
            {"label": "Matched a known object", "value": n_known, "help": GLOSSARY["known_object"]},
            {"label": "Unidentified", "value": n_unid, "help": GLOSSARY["unidentified"]},
        ],
        "tables": [{"title": "Detections", "rows": rows}] if rows else [],
        "observations": items,
    }


def summarise(ctx: ViewContext) -> Summary:
    n_obs, n_tracks, n_known, n_unid = _counts(ctx.observations)
    fields = plural(n_obs, "field")
    if n_tracks == 0:
        return Summary(
            f"No moving-object candidates in {fields} so far.",
            "A quiet field is a normal outcome. It does not mean the search failed.",
            "neutral",
        )
    other = n_tracks - n_known - n_unid  # position matched but speed did not, or no catalogue answer
    found = f"{plural(n_tracks, 'moving-object candidate')} in {fields}: {n_known} matched a known object, {n_unid} unidentified"
    found += f", {other} not conclusively checked." if other else "."
    if n_unid:
        return Summary(
            found,
            f"{plural(n_unid, 'candidate')} not explained by the catalogues queried. That is a lead worth a closer look, "
            "not a discovery.",
            "attention",
        )
    if n_known == n_tracks:
        return Summary(
            found,
            "Every candidate is a known object: the search is finding real asteroids, and nothing new.",
            "good",
        )
    return Summary(
        found,
        "Some candidates could not be checked against the catalogues, so they stay unconfirmed.",
        "neutral",
    )

