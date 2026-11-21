"""The AAVSO Target Tool: which variable stars the AAVSO's observing sections say need data.

The Target Tool (https://targettool.aavso.org/) is the AAVSO's own list of stars in need of observations: alerts and
campaigns, plus lists the observing sections keep (eclipsing binaries, cataclysmic variables, pulsators, long-period
variables ...).  Each row has a requested cadence, a filter or mode, when the star was last observed and a
high-priority flag for alerts and campaigns.

Where the data comes from
    The tool has a documented JSON API (``/TargetTool/api/v1/targets``), but it needs an AAVSO account's API key.
    The same table is also served, whole, on the tool's public page (every section ticked), with no login: that is
    what this module reads, so a person needs no account to see what comes up.  It is one request, cached on this
    computer like every other target list (:func:`core.networks.fetch_text`), and refreshed only when a person asks.
    The page is HTML, so a change to it is the likeliest thing to break this reader: the parser checks the header
    row and refuses a page that does not look like the table (a captive portal or an error page is never cached).
    Moving to the API once a key is available would be sturdier; the row shape below is the same.

What the tool does NOT say
    The current brightness of a star, and (for an eclipsing binary) when its next eclipse is.  The brightness range
    is the catalogue's; the period has no epoch.  Both are said wherever the figures are shown.

Row (``parse_targets`` -> ``list[dict]``, one per star, a star asked for in several filters is merged)::

    {"name", "vsx_oid", "ra_deg", "dec_deg", "constellation", "var_type",
     "max_mag", "max_band", "min_mag", "min_band",     # brightest and faintest, as catalogued (None when not given)
     "period_d", "cadence_d",                          # cadence: the shortest asked for (days)
     "sections": [str],                                # Alert/Campaign, EB, CV, SPP, LPV, ...
     "modes": [str],                                   # what is asked for: V, B, R, I, All, Visual, Spectroscopy ...
     "last_observed_utc": str | None, "status": "on_cadence" | "due_soon" | "overdue" | "never",
     "high_priority": bool, "notes": str, "notices": [{"text", "url"}],
     "requests": [{"mode", "cadence_d", "last_observed_utc", "status", "high_priority"}]}   # one per filter asked for

    ``cadence_d``, ``last_observed_utc``, ``status`` and ``high_priority`` on the row are summed over every request;
    a caller that can serve only some filters should recompute them from ``requests`` (the report does).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from core import networks as core_networks
from core.networks import NetworkError

URL = "https://targettool.aavso.org/TargetTool/default/index"
SOURCE = "aavso_target_tool"
#: The sections ticked on the page.  ``exo`` (exoplanet hosts) is left out: that is the transit plugin's business.
SECTION_FLAGS = ("ac", "cv", "ev", "spv", "lpv", "yso", "het", "misc")
PARAMS = {**{flag: "on" for flag in SECTION_FLAGS}, "settype": "true"}
CACHE_NAME = "aavso_target_tool.html"
DEFAULT_TTL_S = 24 * 3600.0  # the tool changes by the day (last-observed times); a day-old copy is fine for planning
MIN_ROWS = 50  # a real page has thousands; fewer means this is not the table

_STATUS = {"label-success": "on_cadence", "label-warning": "due_soon", "label-danger": "overdue"}
_RA = re.compile(r"(\d+)h\s*(\d+)m\s*([\d.]+)s")
_DEC = re.compile(r"([+-])\s*(\d+)\D+(\d+)\D+([\d.]+)")
_MAG = re.compile(r"^\s*([<>]?)\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]+\w*)?\s*$")


class _Table(HTMLParser):
    """Collects the cells of the results table: text, the attributes of each ``td``, and any links inside it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self.header: list[str] = []
        self._in_body = False
        self._in_head = False
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None
        self._th: list[str] | None = None
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag == "thead":
            self._in_head = True
        elif tag == "tbody":
            self._in_body = True
        elif tag == "th" and self._in_head:
            self._th = []
        elif tag == "tr" and self._in_body:
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"attrs": a, "text": [], "links": [], "classes": [], "titles": []}
        elif self._cell is not None:
            if tag == "a" and a.get("href"):
                self._href = a["href"]
                self._cell["links"].append({"text": "", "url": a["href"]})
            if a.get("class"):
                self._cell["classes"].extend(str(a["class"]).split())
            if a.get("title"):
                self._cell["titles"].append(str(a["title"]))
            if tag in ("p", "br", "div"):
                self._cell["text"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "thead":
            self._in_head = False
        elif tag == "tbody":
            self._in_body = False
        elif tag == "th" and self._th is not None:
            self.header.append(" ".join("".join(self._th).split()))
            self._th = None
        elif tag == "a":
            self._href = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._th is not None:
            self._th.append(data)
        if self._cell is not None:
            self._cell["text"].append(data)
            if self._href is not None and self._cell["links"]:
                self._cell["links"][-1]["text"] += data


def _text(cell: dict[str, Any]) -> str:
    return " ".join("".join(cell["text"]).replace("\xa0", " ").split())


def parse_ra_deg(text: str) -> float | None:
    m = _RA.search(text)
    if not m:
        return None
    return (int(m.group(1)) + int(m.group(2)) / 60.0 + float(m.group(3)) / 3600.0) * 15.0


def parse_dec_deg(text: str) -> float | None:
    m = _DEC.search(unescape(text))
    if not m:
        return None
    value = int(m.group(2)) + int(m.group(3)) / 60.0 + float(m.group(4)) / 3600.0
    return -value if m.group(1) == "-" else value


def parse_mag(text: str) -> tuple[float | None, str | None]:
    """``"10.1 V"`` -> ``(10.1, "V")``; ``"—"`` or anything that is not a magnitude -> ``(None, None)``."""
    m = _MAG.match(text.replace("\u2014", "").replace("&mdash;", ""))
    if not m or not m.group(2):
        return None, None
    return float(m.group(2)), m.group(3)


def _opt_float(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and value > 0 else None


def _utc(cell: dict[str, Any]) -> str | None:
    """The last-observed time (UTC, ISO) from the tooltip on the cell's label (``2026-10-28 02:06:10 UTC``).

    ``None`` for 'No Observation', which has no tooltip.  (The cell's own ``data-sort-title`` is not used: the page
    writes it without quotes, so a time with a space in it does not survive as one value.)
    """
    for title in cell["titles"]:
        m = re.match(r"\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*UTC", title)
        if m:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    return None


def order_of(status: str) -> int:
    """How urgent a status is (higher is more urgent)."""
    return {"never": 3, "overdue": 2, "due_soon": 1, "on_cadence": 0}[status]


def parse_targets(html: str) -> list[dict[str, Any]]:
    """Every star in the Target Tool page, one row per star (see the module docstring).

    Raises :class:`ValueError` when the page is not the table (no header row, too few rows, an unexpected column
    count), so a changed or replaced page is refused instead of read wrongly.
    """
    table = _Table()
    table.feed(html)
    header = [h.lower() for h in table.header]
    wanted = ("star name", "ra", "dec", "var", "max", "min", "period", "cadence", "section", "filter", "last", "priority", "notes")
    if len(header) != 14 or not all(any(w in h for h in header) for w in wanted):
        raise ValueError(f"the results table's header is not the one expected: {table.header[:14]!r}")
    if len(table.rows) < MIN_ROWS:
        raise ValueError(f"only {len(table.rows)} rows; the table has thousands")
    merged: dict[str, dict[str, Any]] = {}
    for cells in table.rows:
        if len(cells) != 14:
            continue
        name = _text(cells[0])
        ra, dec = parse_ra_deg(_text(cells[1])), parse_dec_deg(_text(cells[2]))
        if not name or ra is None or dec is None:
            continue
        max_mag, max_band = parse_mag(_text(cells[5]))
        min_mag, min_band = parse_mag(_text(cells[6]))
        sections = [s.strip() for s in _text(cells[9]).split(",") if s.strip()]
        mode = _text(cells[10])
        cadence = _opt_float(_text(cells[8]))
        last = _utc(cells[11])
        classes = " ".join(cells[11]["classes"])
        status = next((v for k, v in _STATUS.items() if k in classes), "overdue")
        if last is None:
            status = "never"
        oid = re.search(r"oid=(\d+)", cells[0]["links"][0]["url"]) if cells[0]["links"] else None
        notes_cell = cells[13]
        notices = [{"text": " ".join(l["text"].split()), "url": l["url"]} for l in notes_cell["links"] if l["text"].strip()]
        note_text = "; ".join(p.strip() for p in "".join(notes_cell["text"]).split("\n") if p.strip())
        key = name.lower()
        row = merged.get(key)
        if row is None:
            row = merged[key] = {
                "name": name, "vsx_oid": oid.group(1) if oid else None, "ra_deg": ra, "dec_deg": dec,
                "constellation": _text(cells[3]), "var_type": _text(cells[4]),
                "max_mag": max_mag, "max_band": max_band, "min_mag": min_mag, "min_band": min_band,
                "period_d": _opt_float(_text(cells[7])), "cadence_d": cadence, "sections": [], "modes": [],
                "last_observed_utc": None, "status": "never", "high_priority": False, "notes": "", "notices": [],
                "requests": [], "_rank": -1,
            }
        for s in sections:
            if s not in row["sections"]:
                row["sections"].append(s)
        if mode and mode not in row["modes"]:
            row["modes"].append(mode)
        if cadence is not None and (row["cadence_d"] is None or cadence < row["cadence_d"]):
            row["cadence_d"] = cadence
        if last and (row["last_observed_utc"] is None or last > row["last_observed_utc"]):
            row["last_observed_utc"] = last
        if order_of(status) > row["_rank"]:  # the most urgent request wins
            row["_rank"], row["status"] = order_of(status), status
        flagged = cells[12]["attrs"].get("data-sort-ptitle") == "A"
        row["high_priority"] = row["high_priority"] or flagged
        row["requests"].append(
            {"mode": mode, "cadence_d": cadence, "last_observed_utc": last, "status": status, "high_priority": flagged}
        )
        if note_text and note_text not in row["notes"]:
            row["notes"] = (row["notes"] + "; " + note_text) if row["notes"] else note_text
        for n in notices:
            if n not in row["notices"]:
                row["notices"].append(n)
    out = list(merged.values())
    for row in out:
        row.pop("_rank", None)
    return out


def _validate(body: str) -> None:
    parse_targets(body)


def fetch_targets(cache_dir: Path, ttl_s: float, *, cached_only: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The Target Tool's stars and where they came from.

    ``cached_only=True`` never touches the network: it reads the copy on this computer (raises
    :class:`NetworkError` when there is none).  Otherwise a copy younger than ``ttl_s`` is reused, and a failed
    download falls back to an older copy, flagged stale.  ``info`` has ``retrieved_utc``, ``from_cache``,
    ``stale_cache`` and ``problems``.
    """
    path = Path(cache_dir) / CACHE_NAME
    if cached_only:
        if not path.is_file():
            raise NetworkError("the AAVSO Target Tool list has not been downloaded to this computer yet")
        text = path.read_text(encoding="utf-8")
        meta = core_networks._meta(path.stat().st_mtime, from_cache=True, stale=False)
    else:
        text, meta = core_networks.fetch_text(URL, path, ttl_s=ttl_s, params=PARAMS, timeout=90.0, validate=_validate)
    try:
        rows = parse_targets(text)
    except ValueError as exc:
        raise NetworkError(f"the cached AAVSO Target Tool page cannot be read ({exc})") from exc
    return rows, {
        "retrieved_utc": meta["retrieved_utc"], "from_cache": meta["from_cache"], "stale_cache": meta["stale"], "problems": [],
    }
