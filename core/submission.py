"""Upload files: what a mission leaves behind for the field's own database.

A mission's product is the data, formatted the way a collaborative
network ingests it (an AAVSO Exoplanet Database report, an MPC ADES file ...), plus a short log
of how the night went.  Plugins know the formats; this module decides what a file is *allowed to
claim*, so that logic cannot be forgotten by a plugin:

``simulated``          every pixel was synthetic.  Never uploadable.
``replay``             real data, but replayed from a public archive: it is not this observatory's
                       measurement and is already public.  Never uploadable.
``draft``              real data from this telescope, but something blocks an upload (no observer
                       code yet, a failed quality gate, a required attachment missing).
``ready_for_review``   nothing known blocks it.  A person still has to read it and send it:
                       **Astronomo never sends anything**, and there is no code path that could.
``held``               the plugin chose not to write a file, and says why (for example a
                       candidate that has not been independently confirmed).

Every file name carries its status unless it is ``ready_for_review``, and the folder holds a
``manifest.json`` listing each file with its checksum, status and blockers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATUS_SIMULATED = "simulated"
STATUS_REPLAY = "replay"
STATUS_DRAFT = "draft"
STATUS_READY = "ready_for_review"
STATUS_HELD = "held"
STATUSES = (STATUS_SIMULATED, STATUS_REPLAY, STATUS_DRAFT, STATUS_READY, STATUS_HELD)

#: Statuses that can never be uploaded, whatever the plugin says.
NEVER_UPLOAD = {STATUS_SIMULATED, STATUS_REPLAY}

_PREFIX = {STATUS_SIMULATED: "SIMULATED_", STATUS_REPLAY: "REPLAY_", STATUS_DRAFT: "DRAFT_", STATUS_READY: "", STATUS_HELD: ""}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._+-]+")


@dataclass
class SubmissionDraft:
    """What a plugin hands back from :meth:`BaseExperimentPlugin.build_submissions`.

    ``content=None`` means "no file": the draft is *held back*, and ``blockers`` says why.
    ``attachments`` are extra files the destination requires alongside the report (for example a
    plate-solved finder image); they are copied next to it.
    """

    network: str  # the destination, e.g. "AAVSO Exoplanet Database"
    kind: str  # what it is, e.g. "exoplanet_photometry"
    format: str  # the format and version, e.g. "AAVSO Exoplanet report (EXOPLANET)"
    filename: str  # a bare file name, e.g. "WASP-12b_2026-10-21.txt"
    content: str | None
    target: str | None = None
    blockers: list[str] = field(default_factory=list)  # things a person must fix before this can be sent
    notes: list[str] = field(default_factory=list)  # things a reviewer should know
    attachments: list[str] = field(default_factory=list)  # paths to files the destination also wants


def classify(draft: SubmissionDraft, *, simulated: bool, data_source: str) -> str:
    """The status a draft is allowed to have, given where its data came from."""
    if draft.content is None:
        return STATUS_HELD
    if simulated:
        return STATUS_SIMULATED
    if data_source == "archive":
        return STATUS_REPLAY
    return STATUS_DRAFT if draft.blockers else STATUS_READY


def _safe(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", Path(name).name).strip("._")
    return cleaned or "submission"


def write_submissions(
    drafts: list[SubmissionDraft], out_dir: str | Path, *, mission_id: str, simulated: bool, data_source: str
) -> list[dict[str, Any]]:
    """Write every draft that has content, plus ``manifest.json``; returns one record per draft.

    Records have ``file`` (``None`` when held back), ``path``, ``network``, ``kind``, ``format``,
    ``target``, ``status``, ``blockers``, ``notes``, ``attachments``, ``sha256`` and ``sent``
    (always ``False``).  Safe to call again for the same mission: files are overwritten and the
    manifest rewritten.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    used: set[str] = set()
    for draft in drafts:
        status = classify(draft, simulated=simulated, data_source=data_source)
        blockers = list(draft.blockers)
        if status == STATUS_SIMULATED:
            blockers.append("simulated data: this file demonstrates the pipeline and must never be uploaded")
        elif status == STATUS_REPLAY:
            blockers.append("replayed archive data: not this observatory's measurement, and already public")
        record: dict[str, Any] = {
            "file": None, "path": None, "network": draft.network, "kind": draft.kind, "format": draft.format,
            "target": draft.target, "status": status, "blockers": blockers, "notes": list(draft.notes),
            "attachments": [], "sha256": None, "sent": False,
        }
        if draft.content is not None:
            name = _PREFIX[status] + _safe(draft.filename)
            stem, dot, ext = name.rpartition(".")
            n = 1
            while name in used:  # two targets must never overwrite each other
                n += 1
                name = f"{stem}_{n}.{ext}" if dot else f"{name}_{n}"
            used.add(name)
            path = out / name
            path.write_text(draft.content, encoding="utf-8")
            record.update(file=name, path=str(path), sha256=hashlib.sha256(draft.content.encode("utf-8")).hexdigest())
            for src in draft.attachments:
                source = Path(src)
                if not source.is_file():
                    record["blockers"].append(f"required attachment is missing: {source.name}")
                    continue
                dest = out / f"{path.stem}__{_safe(source.name)}"
                dest.write_bytes(source.read_bytes())
                record["attachments"].append(dest.name)
            if record["blockers"] and status == STATUS_READY:  # an attachment went missing just now
                record["status"] = STATUS_DRAFT
        records.append(record)

    manifest = {
        "mission_id": mission_id,
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "simulated": simulated,
        "data_source": data_source,
        "note": "Nothing in this folder has been sent anywhere. A person must review each file and send it.",
        "files": records,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, int]:
    """Counts by status, for log lines and the CLI."""
    counts = {s: 0 for s in STATUSES}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts
