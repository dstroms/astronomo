"""Turn a *document spec* into a Markdown file: the mission log.

A document spec is plain data (see :func:`core.mission_log.build_log`)::

    {"title": str, "abstract": str, "keywords": [str],
     "sections": [{"heading": str, "paragraphs": [str],
                   "tables": [{"caption": str, "columns": [str], "rows": [[str]]}],
                   "figures": [{"path": str, "caption": str}]}]}

The writer is domain-agnostic: it renders whatever sections the caller supplies and adds two
generic sections itself (the observing log, with the weather at the start of each observation,
and a reproducibility table).  The output is one ``<basename>.md`` plus a ``figures/`` folder
beside it, so the log reads in any editor, on GitHub, and as a plain download from the dashboard.
Markdown diffs, searches and copies cleanly, and a person can edit
it before sharing it.
"""

from __future__ import annotations

import logging
import platform
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from config import Settings

log = logging.getLogger(__name__)

#: Packages whose versions go into the reproducibility table.
_VERSIONED = ("numpy", "astropy", "photutils", "astroquery", "langgraph")


def md_cell(value: Any) -> str:
    """One table cell: a single line, with the characters that would break a Markdown table escaped."""
    text = "" if value is None else " ".join(str(value).split())
    return text.replace("\\", "\\\\").replace("|", "\\|") or "--"


def md_text(value: Any) -> str:
    """A paragraph: whitespace collapsed, so a stray line break or blank line cannot split it."""
    return " ".join(("" if value is None else str(value)).split())


@dataclass
class LogResult:
    """Where the log was written."""

    title: str
    path: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "path": self.path, "notes": self.notes}


class LogWriter:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ #
    def write(self, doc: dict, meta: dict, out_dir: str | Path, *, basename: str = "mission_log") -> LogResult:
        """Render ``doc`` (plus the generic sections from ``meta``) to ``out_dir/<basename>.md``."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []

        sections = self._insert_observing_log(list(doc.get("sections", [])), meta)
        lines: list[str] = [f"# {md_text(doc.get('title') or 'Mission log')}", ""]
        lines += [
            f"*{md_text(self._byline(meta))} | {datetime.now(timezone.utc).strftime('%Y-%m-%d')}*",
            "",
        ]
        if meta.get("simulated"):
            lines += ["> **Simulation.** All data in this log are synthetic and demonstrate the pipeline only.", ""]
        if doc.get("abstract"):
            lines += [f"> {md_text(doc['abstract'])}", ""]
        if doc.get("keywords"):
            keywords = ", ".join(md_text(k) for k in doc["keywords"] if k)
            if keywords:
                lines += [f"Keywords: {keywords}", ""]

        fig_no = 0
        for sec in sections:
            lines += [f"## {md_text(sec['heading'])}", ""]
            for paragraph in sec.get("paragraphs", []):
                if md_text(paragraph):
                    lines += [md_text(paragraph), ""]
            for table in sec.get("tables", []):
                lines += self._table(table)
            for fig in sec.get("figures", []):
                src = Path(fig["path"])
                if not src.is_file():
                    note = f"Figure missing, skipped: {src}"
                    log.warning(note)
                    notes.append(note)
                    continue
                fig_no += 1
                (out / "figures").mkdir(exist_ok=True)
                dest = out / "figures" / f"fig{fig_no:02d}_{src.name}"
                shutil.copyfile(src, dest)
                lines += [f"![{md_text(fig.get('caption', ''))}](figures/{dest.name})", "", f"*{md_text(fig.get('caption', ''))}*", ""]

        lines += ["## Reproducibility", "", "| Item | Value |", "| --- | --- |"]
        lines += [f"| {md_cell(k)} | {md_cell(v)} |" for k, v in self._reproducibility(meta)]
        lines.append("")

        path = out / f"{basename}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        log.info("Mission log written: %s", path)
        return LogResult(title=md_text(doc.get("title") or "Mission log"), path=str(path), notes=notes)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _byline(meta: dict) -> str:
        return f"Astronomo ({meta.get('plugin_name', 'unknown plugin')})"

    @staticmethod
    def _table(table: dict) -> list[str]:
        columns = [md_cell(c) for c in table.get("columns", [])]
        if not columns:
            return []
        lines: list[str] = []
        if table.get("caption"):
            lines += [f"**{md_text(table['caption'])}**", ""]
        lines += ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
        for row in table.get("rows", []):
            cells = [md_cell(c) for c in row][: len(columns)]
            cells += ["--"] * (len(columns) - len(cells))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
        return lines

    def _insert_observing_log(self, sections: list[dict], meta: dict) -> list[dict]:
        """Add the generic observing log: where, and the weather at the start of each observation."""
        rows = []
        for rec in meta.get("observations", []):
            tgt, wx, plan = rec.get("target", {}), rec.get("weather") or {}, rec.get("exposure_plan") or {}
            rows.append([
                tgt.get("name", "?"),
                f"{tgt['ra_deg']:.4f}" if isinstance(tgt.get("ra_deg"), (int, float)) else "--",
                f"{tgt['dec_deg']:.4f}" if isinstance(tgt.get("dec_deg"), (int, float)) else "--",
                f"{plan.get('count', '?')} x {plan.get('exposure_s', '?')} s",
                f"gain {plan.get('gain', '?')}, {plan.get('filter', '?')}",
                f"{wx['cloud_cover_pct']:.0f}" if wx.get("cloud_cover_pct") is not None else "--",
                f"{wx['seeing_arcsec']:.1f}" if wx.get("seeing_arcsec") is not None else "--",
                wx.get("source", "--"),
            ])
        site = meta.get("site") or {}
        where = (
            f"Observations were made from {site.get('name', 'the configured site')} "
            f"(latitude {site.get('lat', float('nan')):.3f} deg, longitude {site.get('lon', float('nan')):.3f} deg). "
            "The seeing value is a heuristic proxy from wind data, not a measurement."
        )
        generic = {
            "heading": "Observing log",
            "paragraphs": [where],
            "tables": [{
                "caption": "Observed targets and conditions at the start of each observation.",
                "columns": ["Target", "RA (deg)", "Dec (deg)", "Frames", "Setup", "Cloud (%)",
                            "Seeing proxy (arcsec)", "Weather source"],
                "rows": rows or [["--"] * 8],
            }],
            "figures": [],
        }
        for i, sec in enumerate(sections):
            if str(sec.get("heading", "")).lower() == "observations":
                return sections[: i + 1] + [generic] + sections[i + 1:]
        return sections + [generic]

    @staticmethod
    def _reproducibility(meta: dict) -> list[tuple[str, str]]:
        versions = []
        for dist in _VERSIONED:
            try:
                versions.append(f"{dist} {metadata.version(dist)}")
            except metadata.PackageNotFoundError:
                pass
        llm = meta.get("llm")
        llm_text = (
            f"{llm['provider']}: {llm['model']} ({llm.get('calls', 0)} call(s), {llm.get('total_tokens', 0)} tokens, "
            f"{llm.get('cached_calls', 0)} cached); wrote the observing rationale shown on the dashboard, no text in this log"
            if llm else "none (all text is deterministic)"
        )
        plugin = str(meta.get("plugin_name", "?"))
        if meta.get("plugin_status") == "draft":
            note = meta.get("plugin_status_note")
            plugin += f" (DRAFT, not ready for use{': ' + note if note else ''}; treat this log as a rehearsal)"
        return [
            ("Mission ID", str(meta.get("mission_id", "?"))),
            ("Plugin", plugin),
            ("Simulated", "yes" if meta.get("simulated") else "no"),
            ("Language model", llm_text),
            ("Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
            ("Python", platform.python_version()),
            ("Packages", ", ".join(versions) or "n/a"),
        ]
