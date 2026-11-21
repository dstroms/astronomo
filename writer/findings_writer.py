"""Render a findings spec (:func:`core.findings.build_findings`) as one Markdown page.

The page and its finder image are written into the mission's results folder, next to the upload files and the
mission log.  Plain Markdown on purpose: it reads in any editor, on GitHub and as a download from the dashboard.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from writer.markdown_writer import LogResult, md_cell, md_text

log = logging.getLogger(__name__)


def write_findings(spec: dict[str, Any], out_dir: str | Path, *, basename: str = "findings") -> LogResult:
    """Write ``out_dir/<basename>.md`` (and ``out_dir/figures/`` for the finder image); returns where it went."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    lines = [f"# {md_text(spec['title'])}", "", f"*Astronomo | {datetime.now(timezone.utc).strftime('%Y-%m-%d')}*", ""]
    if spec.get("simulated"):
        lines += ["> **Simulation.** All data here are synthetic and demonstrate the pipeline only.", ""]
    lines += [f"> {md_text(spec['abstract'])}", ""]

    lines += ["## What was measured", ""]
    if spec["rows"]:
        lines += ["| Target | Measured | Quoted error | Compared with |", "| --- | --- | --- | --- |"]
        lines += [
            f"| {md_cell(r['target'])} | {md_cell(r['measured'])} | {md_cell(r['error'])} | {md_cell(r['compared'])} |"
            for r in spec["rows"]
        ]
    else:
        lines.append("Nothing was observed.")
    lines.append("")

    lines += ["## Finder image", ""]
    finder = spec.get("finder") or {}
    src = Path(finder["path"]) if finder.get("path") else None
    if src is not None and src.is_file():
        (out / "figures").mkdir(exist_ok=True)
        dest = out / "figures" / f"finder_{src.name}"
        shutil.copyfile(src, dest)
        caption = md_text(finder.get("caption", ""))
        lines += [f"![{caption}](figures/{dest.name})", "", f"*{caption}*", ""]
    else:
        if src is not None:
            notes.append(f"Finder image missing, skipped: {src}")
            log.warning(notes[-1])
        lines += ["No first-frame view was saved for this mission, so there is no finder image.", ""]

    lines += ["## What the numbers mean", ""] + [f"- {md_text(c)}" for c in spec["caveats"]] + [""]

    lines += ["## Check before sending", ""] + [f"- [ ] {md_text(c)}" for c in spec["checks"]] + [""]

    lines += ["## Files", ""]
    if spec["files"]:
        lines += ["| File | Destination | Status | Blockers | Attached |", "| --- | --- | --- | --- | --- |"]
        lines += [
            f"| {md_cell(f['file'])} | {md_cell(f['destination'])} | {md_cell(f['status'])} | "
            f"{md_cell('; '.join(f['blockers']) or 'none')} | {md_cell(', '.join(f['attachments']) or 'none')} |"
            for f in spec["files"]
        ]
    else:
        lines.append("This science case produced no upload files for this mission.")
    lines.append("")
    if spec.get("mission_log"):
        lines += [f"The full record of the night, including the anomaly screen, is in [{spec['mission_log']}]({spec['mission_log']}).", ""]

    path = out / f"{basename}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Findings summary written: %s", path)
    return LogResult(title=md_text(spec["title"]), path=str(path), notes=notes)
