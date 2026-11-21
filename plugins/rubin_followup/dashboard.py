"""Dashboard view for the Rubin follow-up: per alert, what Rubin saw, what we measured, and whether they agree."""

from __future__ import annotations

from dashboard.plugin_ui import Summary, ViewContext
from dashboard.vocab import plural

LABEL = "Rubin alert follow-up"

_VERDICT = {
    "detected_consistent_with_rubin": ("success", "Detected, consistent with Rubin"),
    "detected_brighter_than_expected": ("warning", "Detected, brighter than Rubin implies"),
    "detected_fainter_than_expected": ("warning", "Detected, fainter than Rubin implies"),
    "not_detected_expected_visible": ("error", "Not detected, but should have been visible"),
    "not_detected_uninformative": ("info", "Not detected (too faint for this stack to say)"),
}


def _fmt(value, spec: str = ".2f") -> str:
    return "--" if value is None else format(value, spec)


def _kinds(observations: list[dict]) -> list[str]:
    return [(((o.get("crossref") or {}).get("entries") or [{}])[0]).get("classification", "") for o in observations]


def _result_rows(entry: dict, exp: dict, meas: dict) -> list[dict]:
    return [
        {"quantity": "Rubin last detection", "value": f"{_fmt(exp.get('last_mag'))} ({exp.get('band', '--')})",
         "note": f"{_fmt(exp.get('age_d'), '.1f')} d before our stack"},
        {"quantity": "Rubin-implied magnitude now", "value": _fmt(exp.get("expected_mag")), "note": exp.get("basis", "--")},
        {"quantity": "Seestar magnitude (G-like)", "value": _fmt(entry.get("mag")) + (f" +/- {_fmt(entry.get('mag_err'))}" if entry.get("mag") is not None else ""),
         "note": f"SNR {_fmt(meas.get('snr'), '.1f')}"},
        {"quantity": "Seestar 5-sigma limit", "value": _fmt(entry.get("limit_mag_5sigma"), ".1f"), "note": "in this stack"},
        {"quantity": "Seestar minus Rubin-implied", "value": _fmt(entry.get("delta_mag"), "+.2f"),
         "note": f"agreement window +/- {_fmt(entry.get('tolerance_mag'))} mag"},
    ]


def evidence(ctx: ViewContext) -> dict:
    """Per-alert comparison as JSON."""
    records = [(o, ((o.get("crossref") or {}).get("entries") or [{}])[0]) for o in ctx.observations]
    kinds = _kinds(ctx.observations)
    if not records:
        return {"captions": ["Waiting for the first alert to be observed and reduced."]}
    items = []
    for obs, entry in records:
        findings, xref = obs.get("findings") or {}, obs.get("crossref") or {}
        plan = obs.get("exposure_plan") or {}
        meas = xref.get("measurement") or {}
        kind = entry.get("classification", "pending")
        _level, label = _VERDICT.get(kind, ("info", kind.replace("_", " ")))
        captions = [
            f"{findings.get('n_frames_used', '?')}/{plan.get('count', '?')} frames of {plan.get('exposure_s', '?')} s, "
            f"{_fmt(findings.get('total_exposure_s'), '.0f')} s stacked, {findings.get('n_sources', '?')} sources in the stack."
        ]
        if xref.get("summary"):
            captions.append(xref["summary"])
        cal = xref.get("calibration") or {}
        if cal.get("status"):
            bits = [f"Gaia calibration: {cal['status']}"]
            if cal.get("zero_point") is not None:
                bits.append(f"zero point {cal['zero_point']:.3f} +/- {_fmt(cal.get('zp_err'), '.3f')} from {cal.get('zp_n')} stars")
            if cal.get("offset_arcsec") is not None:
                bits.append(f"pointing offset {cal['offset_arcsec']:.0f} arcsec")
            captions.append(" | ".join(bits))
        captions.extend(xref.get("notes") or [])
        tables = []
        if entry:
            tables.append({"title": "Compared with Rubin", "rows": _result_rows(entry, xref.get("rubin") or {}, meas)})
        items.append({
            "title": obs.get("target_name") or "Alert",
            "summary": label,
            "captions": captions,
            "tables": tables,
            "warnings": findings.get("warnings") or [],
        })
    return {
        "metrics": [
            {"label": "Alerts followed", "value": len(records), "help": "Sources reported by the Rubin Observatory that we pointed at."},
            {"label": "Detected", "value": sum(k.startswith("detected") for k in kinds)},
            {"label": "Consistent with Rubin", "value": sum(k == "detected_consistent_with_rubin" for k in kinds),
             "help": "Our brightness agrees with what Rubin's last measurements imply for tonight."},
            {"label": "Missing but expected", "value": sum(k == "not_detected_expected_visible" for k in kinds),
             "help": "Not seen even though our stack was deep enough to see it if Rubin's trend held."},
        ],
        "observations": items,
    }


def summarise(ctx: ViewContext) -> Summary:
    kinds = _kinds(ctx.observations)
    n = len(kinds)
    detected = sum(k.startswith("detected") for k in kinds)
    consistent = sum(k == "detected_consistent_with_rubin" for k in kinds)
    missing = sum(k == "not_detected_expected_visible" for k in kinds)
    off = detected - consistent  # brighter or fainter than Rubin implies, or nothing to compare with
    inconclusive = n - detected - missing
    found = f"{plural(n, 'alert')} followed: {detected} detected ({consistent} consistent with Rubin)"
    found += f", {missing} not seen although they should have been visible" if missing else ""
    found += f", {inconclusive} inconclusive." if inconclusive else "."
    if missing:
        return Summary(
            found,
            "Some sources were expected to be visible and were not: they may have faded faster than predicted, moved, "
            "not been real, or the position is off. Worth a look.",
            "attention",
        )
    if off:
        return Summary(
            found,
            "Some detections are brighter or fainter than Rubin's data predict, which can mean a source that is still "
            "changing. Worth a look.",
            "attention",
        )
    if n and consistent == n:
        return Summary(found, "Every alert agrees with what Rubin's data predict.", "good")
    return Summary(
        found,
        "Not enough to compare yet: some stacks were too shallow to say whether the source should be visible.",
        "neutral",
    )

