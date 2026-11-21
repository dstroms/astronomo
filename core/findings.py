"""The findings summary: one plain Markdown page per mission, kept beside the upload files and the mission log.

The mission log says what the telescope did.  The findings summary says what was found and how far to
trust it, for the person who has to decide whether to send a file: what was measured, the error that was
quoted for it, how it compares with what is already published, the first frame as a finder image, and what
a person must check before sending.

The numbers come from the plugin (:meth:`core.base_plugin.BaseExperimentPlugin.result_notes`) and the
standing caveats from the plugin too (:meth:`~core.base_plugin.BaseExperimentPlugin.caveats`), because only
a science case knows what its own numbers do and do not mean.  Core adds what is true of every mission:
nothing is sent, a simulation can never be sent, and every file needs reading first.  Like the mission log,
the page contains no model-written text.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: True of every mission, whatever the science case.  A plugin's own caveats come after these.
GENERIC_CAVEATS = (
    "These are measurements made by a pipeline that has not been checked against a published result on this telescope. "
    "A flagged anomaly is a lead for a person to confirm, never a discovery.",
    "Nothing was sent anywhere. Astronomo only prepares files.",
)

#: What a person must do before sending any file.
GENERIC_CHECKS = (
    "Open each upload file and read it.",
    "Run each file through the destination's own validator. The format was checked against the published specification, "
    "not by uploading it.",
    "Look at the finder image and confirm the star measured is the star you meant to measure.",
    "Resolve every blocker listed for a file. A file with a blocker is a draft.",
)

_NO_ERROR = "This plugin does not quote an error for this result."


def _text(value: Any, default: str = "--") -> str:
    text = " ".join(str(value).split()) if value not in (None, "") else ""
    return text or default


def collect_rows(plugin: Any, observations: list[dict]) -> tuple[list[dict[str, str]], list[str]]:
    """One row per observation from the plugin's ``result_notes``; a plugin that raises costs only its own row.

    Returns ``(rows, problems)``.  A row has ``target``, ``measured``, ``error`` and ``compared``.
    """
    rows: list[dict[str, str]] = []
    problems: list[str] = []
    for obs in observations:
        name = _text((obs.get("target") or {}).get("name"), "?")
        try:
            notes = plugin.result_notes(obs) or {}
        except Exception as exc:  # noqa: BLE001 - a bug in one plugin's wording must not lose the page
            log.exception("result_notes failed for %s", name)
            notes = {}
            problems.append(f"findings: result_notes failed for {name}: {exc}")
        rows.append({
            "target": name,
            "measured": _text(notes.get("measured")),
            "error": _text(notes.get("error"), _NO_ERROR),
            "compared": _text(notes.get("compared")),
        })
    return rows, problems


def collect_caveats(plugin: Any) -> tuple[list[str], list[str]]:
    """The generic caveats followed by the plugin's own, with repeats removed; returns ``(caveats, problems)``."""
    problems: list[str] = []
    try:
        own = [str(c) for c in (plugin.caveats() or []) if str(c).strip()]
    except Exception as exc:  # noqa: BLE001
        log.exception("caveats failed")
        own, problems = [], [f"findings: caveats failed: {exc}"]
    seen: list[str] = []
    for caveat in (*GENERIC_CAVEATS, *own):
        text = " ".join(caveat.split())
        if text not in seen:
            seen.append(text)
    return seen, problems


def build_findings(
    mission: dict[str, Any], *, rows: list[dict[str, str]], caveats: list[str], log_name: str | None = None
) -> dict[str, Any]:
    """The findings page as a spec: a plain dict that :func:`writer.findings_writer.write_findings` renders.

    ``mission`` is the record from ``core.graph._mission_record`` (with the first-frame view attached by
    :func:`core.mission_log.with_preview`).  Nothing here reads a file or the network.
    """
    observations = mission.get("observations") or []
    submissions = mission.get("submissions") or []
    anomalies = mission.get("anomalies") or []
    simulated = bool(mission.get("simulated"))
    archive = mission.get("data_source") == "archive"
    setup = mission.get("setup") or {}

    files = [
        {
            "file": _text(s.get("file"), "(none written)"),
            "destination": _text(s.get("network")),
            "status": _text((s.get("status") or "").replace("_", " ")),
            "blockers": [str(b) for b in (s.get("blockers") or [])],
            "attachments": [str(a) for a in (s.get("attachments") or [])],
        }
        for s in submissions
    ]

    checks = list(GENERIC_CHECKS)
    if simulated:
        checks.insert(0, "This was a simulation. Its files demonstrate the pipeline and can never be sent.")
    elif archive:
        checks.insert(0, "This was a replay of public archive data. It is not a measurement made at this telescope.")
    if anomalies:
        checks.append(
            f"{len(anomalies)} anomaly(ies) reached the threshold. Confirm each one independently "
            f"(see {log_name or 'the mission log'}) before reporting it to anyone."
        )

    if observations:
        abstract = (
            f"{len(observations)} observation(s) for the night of {mission.get('night', '?')}. "
            f"{sum(1 for f in files if f['file'] != '(none written)')} upload file(s). Nothing was sent."
        )
    else:
        abstract = "No target was observed, so there is nothing to report. Nothing was sent."

    preview = setup.get("preview_path")
    return {
        "title": f"Findings, {mission.get('night', '?')}: {mission.get('plugin_name', '?')}",
        "abstract": abstract,
        "simulated": simulated,
        "rows": rows,
        "caveats": caveats,
        "checks": checks,
        "files": files,
        "finder": {
            "path": preview if isinstance(preview, str) and preview else None,
            "caption": (
                "The first frame as saved, stretched so faint structure shows. No stars are marked on it. "
                "Orientation against the Seestar app is unconfirmed."
            ),
        },
        "mission_log": log_name,
    }


def summary_payload(spec: dict[str, Any]) -> dict[str, Any]:
    """The part of the page the dashboard shows inline (stored on the artifact row)."""
    return {"rows": spec["rows"], "caveats": spec["caveats"], "checks": spec["checks"]}
