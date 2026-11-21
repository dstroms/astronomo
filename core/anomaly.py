"""Anomaly screening: which results stand far enough out to flag for a person.

Routine data -- a transit that arrives when the ephemeris says it should, an asteroid that
matches its ephemeris -- belongs in the field's database as an upload file.  An anomaly is
something that genuinely stands out: a timing offset or unexpected dip far outside its
uncertainty, a transient far brighter than its history predicts.  It is flagged in the mission
log for a person to review; nothing is written up and nothing is sent.

A plugin proposes anomalies (:meth:`BaseExperimentPlugin.detect_anomalies`); this module
**enforces the bar**.  An anomaly must carry a finite significance in sigma and must reach
``settings.anomaly_min_sigma`` to count.  Weaker ones are kept and shown in the mission log as
"below threshold", so a person can see what was screened out; they are never flagged as anomalies.

Anomaly (``detect_anomalies`` -> ``list[dict]``)::

    {
      "kind": str,                  # e.g. "timing_offset", "unexpected_dip", "outburst"
      "target": str,                # target name
      "summary": str,               # one plain sentence that states the measurement, no more
      "significance_sigma": float,  # how many sigma the measurement is from the expectation
      "evidence": dict,             # the numbers behind it (JSON-serialisable)
    }

A detected anomaly is a *lead*, not a result: it says "worth a careful look and an independent
confirmation", never "discovery".
"""

from __future__ import annotations

import math
from typing import Any

REQUIRED = ("kind", "target", "summary", "significance_sigma")


def _valid(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, f"anomaly is not a dict ({type(raw).__name__})"
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        return None, f"anomaly is missing {', '.join(missing)}"
    try:
        sigma = float(raw["significance_sigma"])
    except (TypeError, ValueError):
        return None, f"anomaly '{raw.get('kind')}' has a non-numeric significance_sigma"
    if not math.isfinite(sigma):
        return None, f"anomaly '{raw.get('kind')}' has a non-finite significance_sigma"
    return {
        "kind": str(raw["kind"]), "target": str(raw["target"]), "summary": str(raw["summary"]),
        "significance_sigma": sigma, "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {},
    }, None


def screen(raw: list[Any], min_sigma: float) -> tuple[list[dict], list[dict], list[str]]:
    """Split a plugin's proposals into ``(accepted, below_threshold, problems)``.

    ``accepted`` reached ``min_sigma``; ``below_threshold`` is valid but too weak; ``problems``
    are human-readable reasons for entries that were malformed and dropped.
    """
    accepted: list[dict] = []
    below: list[dict] = []
    problems: list[str] = []
    for item in raw or []:
        anomaly, problem = _valid(item)
        if anomaly is None:
            problems.append(str(problem))
            continue
        (accepted if abs(anomaly["significance_sigma"]) >= min_sigma else below).append(anomaly)
    accepted.sort(key=lambda a: -abs(a["significance_sigma"]))
    below.sort(key=lambda a: -abs(a["significance_sigma"]))
    return accepted, below, problems
