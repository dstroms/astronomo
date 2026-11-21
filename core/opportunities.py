"""What a person could take part in: the shape of an *opportunity report*.

The dashboard's Opportunities page answers "what could I observe, and what would it be worth?"
*before* a mission is started.  Core knows no science case, so this module holds only the shape of
the answer and a validator; the plugin that understands the data builds it
(:meth:`core.base_plugin.BaseExperimentPlugin.opportunities`).

Nothing here starts a mission, talks to hardware or sends anything.  An opportunity is a suggestion
a person can turn into a *planned* record (``Database.plan_observation``); a scheduler (roadmap Phase 5)
will be what picks those up.

Report (``opportunities()`` -> ``dict``)::

    {
      "plugin": "transit_finder",
      "label": "Exoplanet transit timing",          # short name for the page's science-case switch
      "summary": str,                               # OPTIONAL: one sentence under the switch, saying what this list is
      "generated_utc": str,                         # when this was worked out
      "horizon_days": int,                          # how far ahead windows were searched
      "site": {"name": str, "lat": float, "lon": float, "elev_m": float, "source": "default" | "environment" | "file"},
      "limits": {str: float},                       # what "reachable" meant (altitude, brightness, horizon ...)
      "data": {                                     # where the target lists came from
          "retrieved_utc": str | None,              # the oldest list used
          "stale_cache": bool,                      # an expired copy was used because a fetch failed
          "from_cache": bool,                       # no network was touched for this report
          "problems": [str],                        # lists that could not be used, and why
          "notice": str,                            # OPTIONAL: a warning a person must see before trusting the figures
      },                                            #   (e.g. a list is down, so estimates may be overstated)
      "n_candidates": int,                          # how many targets the networks named
      "caveats": [str],                             # what this does NOT check (weather, Moon ...)
      "words": {"item": str, "items": str,          # OPTIONAL: what targets and windows are called on the page
                "chance": str, "chances": str},     #   (default: Planet / planets / transit / transits)
      "limits_text": str,                           # OPTIONAL: the sentence under "What this does not check" saying what "reachable" means
      "planning": bool,                             # OPTIONAL (default true): false when a window cannot be turned into a plan
      "impact_guide": {                             # OPTIONAL: what an impact estimate is and is not worth, for people
          "title": str,                             #   (shown in a modal from the estimate's "assumes" box)
          "summary": str,                           #   one or two sentences
          "sections": [{"heading": str, "body": str, "technical": bool}],  # "technical": only in the Technical view
      },
      "opportunities": [Opportunity],               # reachable first, best first, then the rest
    }

Opportunity::

    {
      "id": str,                                    # stable key, unique within the report
      "name": str, "host": str | None,
      "network": dict,                              # the target ``network`` block (core/networks.py)
      "reachable": bool,                            # False: shown anyway, with ``blockers``
      "blockers": [str],                            # plain sentences: why it cannot be observed
      "facts": {str: Any},                          # plugin-chosen display values (brightness, depth ...)
      "detail": str,                                # OPTIONAL: one or two sentences about the target, shown on its card
      "windows": [Window],                          # observable windows in the horizon, soonest first
      "impact": Impact | None,
      "schedule": Schedule,                         # OPTIONAL: is the published schedule for this target out of date?
      "target": dict,                               # base target dict, as ``generate_targets`` would give it
    }

Window::  {"start": iso, "mid": iso, "end": iso, "epoch": int | None, "mid_err_min": float | None}

Schedule (a plain verdict next to the numbers behind it)::

    {
      "verdict": "up_to_date" | "getting_stale" | "out_of_date" | "unknown",
      "headline": str,                              # a sentence WITHOUT the verdict word; the page puts it in front
      "reference_utc": str | None,                  # the date the published schedule is counted from. NOT necessarily the
                                                    #   last observation: say so wherever you show it
      "uncertainty_min": float | None,              # how uncertain the predicted time is now
      "provisional": bool,                          # the verdict may change once a missing list loads
    }

Impact (an *estimate*, never a claim)::

    {
      "level": "high" | "medium" | "low" | "unknown",
      "headline": str,                              # one sentence a person can read
      "figures": {str: float | None},               # the numbers behind it
      "assumptions": [str],                         # everything the estimate takes on trust
    }
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

IMPACT_LEVELS = ("high", "medium", "low", "unknown")
SCHEDULE_VERDICTS = ("up_to_date", "getting_stale", "out_of_date", "unknown")
WORD_KEYS = ("item", "items", "chance", "chances")


class OpportunityError(ValueError):
    """Raised when a plugin returns an opportunity report that violates the contract."""


def _iso(value: Any, where: str) -> None:
    try:
        datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise OpportunityError(f"{where} is not an ISO-8601 time: {value!r}") from exc


def check_opportunity(op: Any) -> dict[str, Any]:
    """Validate one opportunity; returns it unchanged or raises :class:`OpportunityError`."""
    from core.networks import check_network_block

    if not isinstance(op, dict):
        raise OpportunityError("an opportunity must be a dict")
    for key in ("id", "name"):
        if not isinstance(op.get(key), str) or not op[key]:
            raise OpportunityError(f"opportunity is missing a text '{key}'")
    name = op["name"]
    try:
        check_network_block(name, op.get("network"))
    except ValueError as exc:
        raise OpportunityError(str(exc)) from exc
    if not isinstance(op.get("reachable"), bool):
        raise OpportunityError(f"opportunity '{name}': 'reachable' must be true or false")
    blockers = op.get("blockers", [])
    if not isinstance(blockers, list) or not all(isinstance(b, str) and b for b in blockers):
        raise OpportunityError(f"opportunity '{name}': 'blockers' must be a list of sentences")
    if not op["reachable"] and not blockers:
        raise OpportunityError(f"opportunity '{name}' is unreachable but says nothing about why")
    windows = op.get("windows", [])
    if not isinstance(windows, list):
        raise OpportunityError(f"opportunity '{name}': 'windows' must be a list")
    if windows and not op["reachable"]:
        raise OpportunityError(f"opportunity '{name}' is unreachable yet lists observable windows")
    previous: str | None = None
    for i, w in enumerate(windows):
        if not isinstance(w, dict):
            raise OpportunityError(f"opportunity '{name}': window #{i} is not a dict")
        for key in ("start", "mid", "end"):
            _iso(w.get(key), f"opportunity '{name}' window #{i} '{key}'")
        if not (datetime.fromisoformat(w["start"]) <= datetime.fromisoformat(w["mid"]) <= datetime.fromisoformat(w["end"])):
            raise OpportunityError(f"opportunity '{name}' window #{i} must satisfy start <= mid <= end")
        if previous is not None and datetime.fromisoformat(w["start"]) < datetime.fromisoformat(previous):
            raise OpportunityError(f"opportunity '{name}': windows must be soonest first")
        previous = w["start"]
    impact = op.get("impact")
    if impact is not None:
        if not isinstance(impact, dict) or impact.get("level") not in IMPACT_LEVELS:
            raise OpportunityError(f"opportunity '{name}': impact.level must be one of {', '.join(IMPACT_LEVELS)}")
        if not isinstance(impact.get("headline"), str) or not impact["headline"].strip():
            raise OpportunityError(f"opportunity '{name}': impact.headline must be a sentence")
        assumptions = impact.get("assumptions")
        if not isinstance(assumptions, list) or not assumptions or not all(isinstance(a, str) and a for a in assumptions):
            raise OpportunityError(
                f"opportunity '{name}': an impact estimate must list its assumptions (an unstated assumption is a claim)"
            )
    schedule = op.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict) or schedule.get("verdict") not in SCHEDULE_VERDICTS:
            raise OpportunityError(f"opportunity '{name}': schedule.verdict must be one of {', '.join(SCHEDULE_VERDICTS)}")
        if not isinstance(schedule.get("headline"), str) or not schedule["headline"].strip():
            raise OpportunityError(f"opportunity '{name}': schedule.headline must be a sentence")
        if schedule.get("reference_utc") is not None:
            _iso(schedule["reference_utc"], f"opportunity '{name}' schedule.reference_utc")
    if op.get("detail") is not None and (not isinstance(op["detail"], str) or not op["detail"].strip()):
        raise OpportunityError(f"opportunity '{name}': 'detail' must be a sentence when present")
    if not isinstance(op.get("target"), dict) or not {"name", "ra_deg", "dec_deg"} <= set(op["target"]):
        raise OpportunityError(f"opportunity '{name}': 'target' needs name, ra_deg and dec_deg")
    return op


def _check_guide(guide: Any) -> None:
    """The optional ``impact_guide``: a title, a summary and sections of text (nothing else is drawn)."""
    if not isinstance(guide, dict):
        raise OpportunityError("'impact_guide' must be a dict")
    for key in ("title", "summary"):
        if not isinstance(guide.get(key), str) or not guide[key].strip():
            raise OpportunityError(f"'impact_guide' needs a text '{key}'")
    sections = guide.get("sections")
    if not isinstance(sections, list) or not sections:
        raise OpportunityError("'impact_guide' needs a non-empty list of 'sections'")
    for sec in sections:
        if not isinstance(sec, dict) or not all(isinstance(sec.get(k), str) and sec[k].strip() for k in ("heading", "body")):
            raise OpportunityError("every 'impact_guide' section needs a text 'heading' and 'body'")
        if not isinstance(sec.get("technical", False), bool):
            raise OpportunityError("a section's 'technical' flag must be true or false")


def check_report(report: Any) -> dict[str, Any]:
    """Validate a whole report (every opportunity, unique ids); returns it unchanged or raises."""
    if not isinstance(report, dict):
        raise OpportunityError("an opportunity report must be a dict")
    for key in ("plugin", "label"):
        if not isinstance(report.get(key), str) or not report[key]:
            raise OpportunityError(f"report is missing a text '{key}'")
    _iso(report.get("generated_utc"), "report 'generated_utc'")
    if not isinstance(report.get("horizon_days"), int) or report["horizon_days"] < 1:
        raise OpportunityError("report 'horizon_days' must be a positive whole number")
    if not isinstance(report.get("data"), dict) or "problems" not in report["data"]:
        raise OpportunityError("report 'data' must say where the lists came from (with 'problems')")
    notice = report["data"].get("notice")
    if notice is not None and (not isinstance(notice, str) or not notice.strip()):
        raise OpportunityError("report data 'notice' must be a sentence when present")
    if report.get("impact_guide") is not None:
        _check_guide(report["impact_guide"])
    words = report.get("words")
    if words is not None and (
        not isinstance(words, dict) or not words or not all(k in WORD_KEYS and isinstance(v, str) and v for k, v in words.items())
    ):
        raise OpportunityError(f"report 'words' must be text for some of {', '.join(WORD_KEYS)}")
    for key in ("limits_text", "summary"):
        if report.get(key) is not None and (not isinstance(report[key], str) or not report[key].strip()):
            raise OpportunityError(f"report '{key}' must be a sentence when present")
    if report.get("planning") is not None and not isinstance(report["planning"], bool):
        raise OpportunityError("report 'planning' must be true or false")
    ops = report.get("opportunities")
    if not isinstance(ops, list):
        raise OpportunityError("report 'opportunities' must be a list")
    seen: set[str] = set()
    for op in ops:
        check_opportunity(op)
        if op["id"] in seen:
            raise OpportunityError(f"duplicate opportunity id '{op['id']}'")
        seen.add(op["id"])
    return report
