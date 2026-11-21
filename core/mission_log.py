"""The automated mission log: a short, factual record of one mission, written every time.

It answers "what did the telescope do and what came of it" for a person reading the folder the
next morning: which targets were chosen and by which network, what each observation found, which
upload files were produced and what blocks them, and what the anomaly screen decided.  It is built
only from the mission record, so it is the same for every science case, and it contains no
model-written text.

The result is a *document spec* (title, sections, tables, figures) that :class:`writer.LogWriter`
renders as a Markdown file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def with_preview(mission: dict[str, Any], mission_dir: Path) -> dict[str, Any]:
    """Point the log at the operator's first-frame view when that file is still on disk."""
    setup = dict(mission.get("setup") or {})
    name = setup.get("preview")
    if not isinstance(name, str) or not name:
        return mission
    path = Path(name)
    if not path.is_file():
        path = mission_dir / name
    if not path.is_file():
        return mission
    return {**mission, "setup": {**setup, "preview_path": str(path)}}


def _cell(value: Any, limit: int = 140) -> str:
    text = " ".join(str(value).split()) if value not in (None, "") else "--"
    return text if len(text) <= limit else text[: limit - 1] + "..."


def build_log(mission: dict[str, Any]) -> dict[str, Any]:
    """Document spec for the mission log.

    ``mission`` is the record built by ``core.graph._mission_record``: ``mission_id``,
    ``plugin_name``, ``hypothesis``, ``simulated``, ``targets``, ``observations``, ``status``,
    ``night`` (the date this log is for), ``submissions`` (records from
    :func:`core.submission.write_submissions`), ``anomalies``, ``below_threshold`` and ``errors``.
    """
    targets = mission.get("targets") or []
    observations = mission.get("observations") or []
    submissions = mission.get("submissions") or []
    anomalies = mission.get("anomalies") or []
    below = mission.get("below_threshold") or []
    errors = mission.get("errors") or []
    skipped = mission.get("skipped") or []
    status = mission.get("status") or "UNKNOWN"
    n_ready = sum(1 for s in submissions if s["status"] == "ready_for_review")
    n_files = sum(1 for s in submissions if s.get("file"))

    summary = [
        f"Mission {mission.get('mission_id', '?')} ({mission.get('plugin_name', '?')}) ended {status} for the night of "
        f"{mission.get('night', '?')}. {len(observations)} of {len(targets)} planned target(s) were observed.",
        mission.get("hypothesis") or "",
    ]
    if skipped:
        summary.append(f"{len(skipped)} target(s) were not observed (see Targets not observed).")
    if mission.get("simulated"):
        summary.insert(1, "This was a simulation: every image and catalogue value is synthetic.")
    elif mission.get("data_source") == "archive":
        summary.insert(1, "This was a replay of public archive data, not a night at this telescope.")

    plan_rows = []
    for t in targets:
        net = t.get("network") or {}
        plan_rows.append([
            _cell(t.get("name")), _cell(", ".join(net.get("sources") or []) or "operator / built-in"),
            _cell((net.get("task") or "manual").replace("_", " ")), _cell(net.get("reason") or "chosen by the plugin", 220),
            _cell(net.get("score")),
        ])

    obs_rows = []
    for rec in observations:
        plan = rec.get("exposure_plan") or {}
        frames = f"{plan.get('count', '?')} x {plan.get('exposure_s', '?')} s" if plan else "--"
        obs_rows.append([
            _cell((rec.get("target") or {}).get("name")), frames,
            _cell((rec.get("findings") or {}).get("summary"), 260), _cell((rec.get("crossref") or {}).get("summary"), 260),
        ])

    out_rows = [
        [_cell(s.get("file") or "(none written)"), _cell(s["network"]), _cell(s["format"]), _cell(s["status"].replace("_", " ")),
         _cell("; ".join(s["blockers"]) or "none", 260)]
        for s in submissions
    ]
    outputs = [
        f"{n_files} upload file(s) written, {n_ready} ready for review. Nothing was sent anywhere: a person must read each file "
        "and send it. Files marked simulated or replay can never be sent." if submissions else
        "This science case produced no upload files for this mission.",
    ]

    screening: list[str] = []
    if anomalies:
        screening.append(
            f"{len(anomalies)} anomaly(ies) reached the {mission.get('anomaly_min_sigma', '?')} sigma threshold. "
            "Each is a lead that needs independent confirmation, not a result."
        )
        screening += [f"{a['kind'].replace('_', ' ')} on {a['target']} ({a['significance_sigma']:.1f} sigma): {a['summary']}" for a in anomalies]
    else:
        screening.append(f"No anomaly reached the {mission.get('anomaly_min_sigma', '?')} sigma threshold.")
    if below:
        screening.append("Below the threshold (screened out, listed so they can be checked):")
        screening += [f"{a['kind'].replace('_', ' ')} on {a['target']} ({a['significance_sigma']:.1f} sigma): {a['summary']}" for a in below]

    setup = mission.get("setup") or {}
    setup_section: list[dict[str, Any]] = []
    if setup.get("checks"):
        paragraphs = [
            f"First-frame checks on {_cell(setup.get('target'))} ({'all passed or advisory' if setup.get('ok') else 'FAILED'}).  "
            "The thresholds are first-light guesses to be re-set from real frames."
        ]
        figures: list[dict[str, str]] = []
        if setup.get("preview_path"):
            paragraphs.append(
                "A stretched view of that frame was saved for the operator before the rest of the sequence. "
                "It is the saved pixels, so a tree, a roof, dew or cloud can be seen. "
                "The checks decide whether the sequence stops. Orientation against the Seestar app is unconfirmed."
            )
            figures.append({
                "path": setup["preview_path"],
                "caption": (
                    "First frame, stretched so faint structure shows. "
                    "Orientation against the Seestar app is unconfirmed."
                ),
            })
        setup_section = [{
            "heading": "Telescope setup",
            "paragraphs": paragraphs,
            "tables": [{
                "caption": "Focus, pointing and calibration.", "columns": ["Check", "Result", "Detail"],
                "rows": [[_cell(c["name"]), _cell(c["status"]), _cell(c["detail"], 320)] for c in setup["checks"]],
            }],
            "figures": figures,
        }]

    sections: list[dict[str, Any]] = [
        {"heading": "Summary", "paragraphs": [p for p in summary if p], "tables": [], "figures": []},
        {
            "heading": "Targets and network tasks",
            "paragraphs": ["Where each target came from and why it was chosen."],
            "tables": [{
                "caption": "Planned targets.", "columns": ["Target", "Source", "Task", "Why", "Score"],
                "rows": plan_rows or [["--"] * 5],
            }],
            "figures": [],
        },
        {
            "heading": "Observations",
            "paragraphs": ["What each observation found, as the pipeline reported it."],
            "tables": [{
                "caption": "Observations.", "columns": ["Target", "Frames", "Result", "Cross-reference"],
                "rows": obs_rows or [["--"] * 4],
            }],
            "figures": [],
        },
        {
            "heading": "Upload files",
            "paragraphs": outputs,
            "tables": [{
                "caption": "Files prepared for the field's databases.", "columns": ["File", "Destination", "Format", "Status", "Blockers"],
                "rows": out_rows or [["--"] * 5],
            }] if submissions else [],
            "figures": [],
        },
        {"heading": "Anomaly screening", "paragraphs": screening, "tables": [], "figures": []},
        *setup_section,
    ]
    credits: list[str] = []
    for rec in observations:  # a replayed public data set must be credited wherever its results appear
        credit = (rec.get("archive") or {}).get("credit")
        if credit and credit not in credits:
            credits.append(credit)
    if credits:
        sections.append({"heading": "Acknowledgements", "paragraphs": credits, "tables": [], "figures": []})
    if skipped:
        sections.append({
            "heading": "Targets not observed",
            "paragraphs": ["A target is skipped, not retried, when its own observing window has closed; the reason is given for each."],
            "tables": [{
                "caption": "Targets that were skipped.", "columns": ["Target", "Reason"],
                "rows": [[_cell(s.get("name")), _cell(s.get("reason"), 300)] for s in skipped],
            }],
            "figures": [],
        })
    if errors:
        sections.append({"heading": "Errors and notes", "paragraphs": [_cell(e, 400) for e in errors], "tables": [], "figures": []})

    return {
        "title": f"Mission log, {mission.get('night', '?')}: {mission.get('plugin_name', '?')}",
        "abstract": (
            f"{status}: {len(observations)} observation(s), {n_files} upload file(s) ({n_ready} ready for review), "
            f"{len(anomalies)} anomaly(ies) above threshold."
        ),
        "keywords": ["mission log", str(mission.get("plugin_name", ""))],
        "sections": sections,
    }
