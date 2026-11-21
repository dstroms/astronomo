"""Writers for the formats collaborative networks ingest.

Pure functions from numbers to text.  They know the *syntax* of each format and refuse to emit
anything the format's published specification forbids, but they know nothing about any science
case: plugins decide what goes in, :mod:`core.submission` decides what a finished file may claim.

Formats (each checked against the specification it names, not by uploading to the service):

``aavso_exoplanet_report``  AAVSO Exoplanet Report File Format v0.62 (one observation per file: a
                            whole transit time series).  This is what the AAVSO Exoplanet Database
                            ingests, and the route NASA Exoplanet Watch observers use.
``aavso_extended_report``   AAVSO Extended File Format v1.2 (one star measurement per line), used
                            for variable-star and transient photometry.
``ades_psv``                IAU ADES 2017 pipe-separated values, the Minor Planet Center's
                            preferred way to receive astrometry.

Every writer raises :class:`FormatError` for input the format cannot represent, so a bad value is
caught when the file is built and not when a person tries to upload it.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


class FormatError(ValueError):
    """The data cannot be written in this format."""


_NA = "na"
_DELIMS = {",": ",", ";": ";", "|": "|", "!": "!", ":": ":", "/": "/", "?": "?", "tab": "\t"}
_BINNING = {"1x1", "2x2", "3x3", "4x4"}
_EXO_DATE_TYPES = {"JD_UTC", "HJD_UTC", "BJD_UTC", "BJD_TT", "BJD_TDB"}
_MEASUREMENT_TYPES = {"Rnflux", "Dmag"}
_OBS_CODE = re.compile(r"^[A-Za-z0-9]{1,5}$")
_FILTER = re.compile(r"^[A-Za-z0-9]{1,5}$")


def _one_line(text: Any, limit: int | None = None) -> str:
    """Collapse whitespace so a value can never spill onto another line of the file."""
    s = " ".join(str(text).split())
    return s if limit is None else s[:limit]


def _finite(value: Any, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError) as exc:
        raise FormatError(f"{what} is not a number: {value!r}") from exc
    if not math.isfinite(x):
        raise FormatError(f"{what} is not finite: {value!r}")
    return x


def _header(lines: Iterable[tuple[str, Any]]) -> list[str]:
    return [f"#{k}={_one_line(v)}" for k, v in lines]


# --------------------------------------------------------------------------- #
# AAVSO Exoplanet Report (v0.62)
# --------------------------------------------------------------------------- #
def aavso_exoplanet_report(
    *,
    obs_code: str,
    software: str,
    date_type: str,
    star_name: str,
    exoplanet_name: str,
    exposure_s: float,
    filter_name: str,
    measurement_type: str,
    rows: Sequence[Sequence[float]],
    obstype: str = "CCD",
    binning: str = "1x1",
    detrend_names: Sequence[str] = (),
    ra_deg: float | None = None,
    dec_deg: float | None = None,
    secondary_obs_codes: Sequence[str] = (),
    priors: str | None = None,
    results: str | None = None,
    notes: str | None = None,
    delim: str = ",",
) -> str:
    """One AAVSO Exoplanet Report: a header of ``#KEY=value`` lines, then ``DATE,DIFF,ERR[,detrend...]`` rows.

    ``rows`` are ``(date, difference, error, *detrend)``; the date is the **mid-exposure** time in the
    scale named by ``date_type``.  The AAVSO wants raw (not detrended) values, so ``detrend_names``
    only names extra columns the caller really has.  ``ra_deg``/``dec_deg`` are written (J2000) for
    stars the service cannot resolve by name; pass them whenever the name might be unusual.
    """
    if not _OBS_CODE.match(obs_code or ""):
        raise FormatError(f"AAVSO observer code must be 1-5 letters or digits, not {obs_code!r}")
    if date_type not in _EXO_DATE_TYPES:
        raise FormatError(f"#DATE_TYPE must be one of {', '.join(sorted(_EXO_DATE_TYPES))}, not {date_type!r}")
    if obstype not in ("CCD", "DSLR"):
        raise FormatError(f"#OBSTYPE must be CCD or DSLR (code a CMOS camera as CCD), not {obstype!r}")
    if binning not in _BINNING:
        raise FormatError(f"#BINNING must be one of {', '.join(sorted(_BINNING))}, not {binning!r}")
    if measurement_type not in _MEASUREMENT_TYPES:
        raise FormatError(f"#MEASUREMENT_TYPE must be one of {', '.join(sorted(_MEASUREMENT_TYPES))}, not {measurement_type!r}")
    if not _FILTER.match(filter_name or ""):
        raise FormatError(f"#FILTER must be an AAVSO filter short name (CV for clear), not {filter_name!r}")
    if delim not in _DELIMS:
        raise FormatError(f"#DELIM must be one of {', '.join(_DELIMS)}, not {delim!r}")
    if not star_name or not exoplanet_name:
        raise FormatError("a star name and an exoplanet name are required")
    exposure = _finite(exposure_s, "#EXPOSURE_TIME")
    if not 0 < exposure <= 600:
        raise FormatError(f"#EXPOSURE_TIME must be in (0, 600] s, not {exposure:g}")
    if len(detrend_names) > 4:
        raise FormatError("at most four detrend parameters are allowed")
    if not rows:
        raise FormatError("a report needs at least one measurement")
    sep = _DELIMS[delim]
    n_cols = 3 + len(detrend_names)

    head = [
        ("TYPE", "EXOPLANET"),
        ("OBSCODE", obs_code),
        ("SOFTWARE", _one_line(software, 255)),
        ("DELIM", delim if delim != "tab" else "tab"),
        ("DATE_TYPE", date_type),
        ("OBSTYPE", obstype),
        ("STAR_NAME", _one_line(star_name, 100)),
    ]
    if ra_deg is not None and dec_deg is not None:
        head += [("RA", f"{_finite(ra_deg, 'RA'):.6f}"), ("DEC", f"{_finite(dec_deg, 'DEC'):+.6f}"), ("EPOCH", "J2000")]
    head += [
        ("EXOPLANET_NAME", _one_line(exoplanet_name, 100)),
        ("BINNING", binning),
        ("EXPOSURE_TIME", f"{exposure:g}"),
        ("FILTER", filter_name),
        ("DETREND_PARAMETERS", ",".join(_one_line(n) for n in detrend_names)),
        ("MEASUREMENT_TYPE", measurement_type),
    ]
    if secondary_obs_codes:
        head.append(("SECONDARY_OBSCODES", ",".join(secondary_obs_codes)))
    for key, value in (("PRIORS", priors), ("RESULTS", results), ("NOTES", notes)):
        if value:
            head.append((key, value))

    out = _header(head)
    out.append("#" + sep.join(["DATE", "DIFF", "ERR", *[_one_line(n).upper() for n in detrend_names]]))
    for i, row in enumerate(rows):
        if len(row) != n_cols:
            raise FormatError(f"row {i} has {len(row)} values; the header declares {n_cols} columns")
        date = _finite(row[0], f"row {i} date")
        diff = _finite(row[1], f"row {i} difference")
        err = row[2]
        cells = [f"{date:.8f}", f"{diff:.6f}", _NA if err is None else f"{_finite(err, f'row {i} error'):.6f}"]
        cells += [_NA if v is None else f"{_finite(v, f'row {i} detrend'):.6f}" for v in row[3:]]
        out.append(sep.join(cells))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# AAVSO Extended (v1.2)
# --------------------------------------------------------------------------- #
EXTENDED_COLUMNS = ("NAME", "DATE", "MAG", "MERR", "FILT", "TRANS", "MTYPE", "CNAME", "CMAG", "KNAME", "KMAG", "AMASS", "GROUP", "CHART", "NOTES")
_EXT_FILTERS = {
    "U", "B", "V", "R", "I", "J", "H", "K", "TG", "TB", "TR", "CV", "CR", "SZ", "SU", "SG", "SR", "SI",
    "STU", "STV", "STB", "STY", "STHBW", "STHBN", "MA", "MB", "MI", "ZS", "Y", "HA", "HAC",
}


def _ext_cell(value: Any, limit: int, sep: str, what: str) -> str:
    if value is None or value == "":
        return _NA
    text = _one_line(value)
    if sep in text:
        text = text.replace(sep, ";" if sep != ";" else ",")  # the delimiter must not appear inside a field
    if "#" in text and what != "NOTES":
        raise FormatError(f"{what} may not contain '#': {value!r}")
    return text[:limit] if what != "NOTES" else text


def aavso_extended_report(
    *,
    obs_code: str,
    software: str,
    rows: Sequence[dict[str, Any]],
    obstype: str = "CCD",
    delim: str = ",",
) -> str:
    """An AAVSO Extended report: ``#`` header, then one line per measurement (dates are JD, mid-exposure).

    Each row is a dict with ``name``, ``jd``, ``mag``, ``merr`` (or ``None``), ``filt`` and optionally
    ``trans`` (bool, default False), ``mtype`` ("STD" | "DIF", default "STD"), ``cname``, ``cmag``,
    ``kname``, ``kmag``, ``amass``, ``group``, ``chart`` and ``notes``.  ``mag`` may be a number or a
    ``("<", number)`` pair for a fainter-than limit.  Missing optional values are written ``na``.
    """
    if not _OBS_CODE.match(obs_code or ""):
        raise FormatError(f"AAVSO observer code must be 1-5 letters or digits, not {obs_code!r}")
    if obstype not in ("CCD", "DSLR", "PEP"):
        raise FormatError(f"#OBSTYPE must be CCD, DSLR or PEP, not {obstype!r}")
    if delim not in _DELIMS or delim in ("|",):
        raise FormatError(f"#DELIM must be one of {', '.join(d for d in _DELIMS if d != '|')}, not {delim!r}")
    if not rows:
        raise FormatError("a report needs at least one measurement")
    sep = _DELIMS[delim]

    out = _header([
        ("TYPE", "EXTENDED"), ("OBSCODE", obs_code), ("SOFTWARE", _one_line(software, 255)),
        ("DELIM", delim if delim != "tab" else "tab"), ("DATE", "JD"), ("OBSTYPE", obstype),
    ])
    out.append("#" + sep.join(EXTENDED_COLUMNS))
    for i, r in enumerate(rows):
        name = _ext_cell(r.get("name"), 30, sep, "NAME")
        if name == _NA:
            raise FormatError(f"row {i} has no star name")
        filt = str(r.get("filt", "")).upper()
        if filt not in _EXT_FILTERS:
            raise FormatError(f"row {i} filter {r.get('filt')!r} is not an AAVSO Extended filter")
        mag = r.get("mag")
        prefix = ""
        if isinstance(mag, (tuple, list)):
            prefix, mag = str(mag[0]), mag[1]
            if prefix != "<":
                raise FormatError(f"row {i}: only '<' (fainter than) may precede a magnitude")
        mag_text = f"{prefix}{_finite(mag, f'row {i} magnitude'):.3f}"
        if len(mag_text) > 8:
            raise FormatError(f"row {i} magnitude {mag_text} exceeds 8 characters")
        merr = r.get("merr")
        merr_text = _NA if merr is None else f"{_finite(merr, f'row {i} merr'):.3f}"[:6]
        mtype = str(r.get("mtype", "STD")).upper()
        if mtype not in ("STD", "DIF", "ABS"):
            raise FormatError(f"row {i}: MTYPE must be STD or DIF, not {mtype!r}")
        if mtype == "DIF" and not r.get("cname"):
            raise FormatError(f"row {i}: a differential magnitude needs the comparison star name (CNAME)")
        jd = _finite(r.get("jd"), f"row {i} date")
        mag_cell = lambda key, limit: _NA if r.get(key) is None else f"{_finite(r[key], f'row {i} {key}'):.3f}"[:limit]  # noqa: E731
        cells = [
            name, f"{jd:.5f}"[:16], mag_text, merr_text, filt, "YES" if r.get("trans") else "NO", mtype,
            _ext_cell(r.get("cname"), 20, sep, "CNAME"), mag_cell("cmag", 8),
            _ext_cell(r.get("kname"), 20, sep, "KNAME"), mag_cell("kmag", 8),
            _NA if r.get("amass") is None else f"{_finite(r['amass'], f'row {i} amass'):.3f}"[:7],
            _ext_cell(r.get("group"), 5, sep, "GROUP"), _ext_cell(r.get("chart"), 20, sep, "CHART"),
            _ext_cell(r.get("notes"), 4000, sep, "NOTES"),
        ]
        out.append(sep.join(cells))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# ADES 2017 PSV (Minor Planet Center)
# --------------------------------------------------------------------------- #
ADES_COLUMNS = ("permID", "provID", "trkSub", "mode", "stn", "obsTime", "ra", "dec", "rmsRA", "rmsDec", "astCat", "notes", "remarks")
_STN = re.compile(r"^[A-Za-z0-9]{3}$")
_PERM_ID = re.compile(r"^[0-9]+[PCDXAI]?$")  # 433, 4, 126P
_PROV_ID = re.compile(r"^(?:[PCDXAI]/)?\d{4} [A-Z]{1,2}\d{0,3}$")  # 2019 AB12, C/2024 J3
#: ADES star-catalog codes for the catalogues the harness can calibrate against.
ADES_CATALOGUES = {"gaia-dr3": "Gaia3", "gaia-dr2": "Gaia2"}


def ades_obs_time(when: datetime) -> str:
    """UTC ``YYYY-MM-DDThh:mm:ss.sssZ`` (the mid-exposure time)."""
    if when.tzinfo is None:
        raise FormatError("an observation time must carry a timezone")
    t = when.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def ades_psv(
    *,
    station: str,
    submitter: str,
    observations: Sequence[dict[str, Any]],
    telescope: dict[str, Any] | None = None,
    software: str = "Astronomo",
) -> str:
    """An ADES 2017 PSV optical-astrometry file for one tracklet or several.

    Each observation is a dict with ``perm_id`` or ``prov_id`` (the object's published designation; give neither for
    an object that has none yet, such as a NEO Confirmation Page candidate, which is then identified by ``trk_sub``
    alone), ``trk_sub`` (the tracklet label, the NEOCP temporary designation for such an object), ``obs_time`` (timezone-aware datetime, UTC
    mid-exposure), ``ra_deg``, ``dec_deg`` (J2000, decimal degrees), ``rms_ra_arcsec``,
    ``rms_dec_arcsec`` (1-sigma, RA*cos(dec)), ``ast_cat`` (an ADES catalogue code such as ``Gaia3``) and
    optionally ``notes`` and ``remarks``.  ``telescope`` may give ``design``, ``aperture`` (m),
    ``fRatio`` and ``detector``.  Magnitudes are deliberately not supported: the harness's
    photometry is instrumental, and the MPC wants calibrated magnitudes or none.
    """
    if not _STN.match(station or ""):
        raise FormatError(f"MPC station code must be three letters or digits, not {station!r}")
    if not submitter or "|" in submitter:
        raise FormatError("a submitter name is required and may not contain '|'")
    if not observations:
        raise FormatError("an ADES file needs at least one observation")
    tel = {"detector": "CCD", **(telescope or {})}
    lines = [
        "# version=2017",
        "# observatory", f"! mpcCode {station}",
        "# submitter", f"! name {_one_line(submitter)}",
        "# observers", f"! name {_one_line(submitter)}",
        "# measurers", f"! name {_one_line(submitter)}",
        "# telescope",
    ]
    for key in ("design", "aperture", "fRatio", "detector"):
        if tel.get(key) not in (None, ""):
            lines.append(f"! {key} {_one_line(tel[key])}")
    lines += ["# software", f"! astrometry {_one_line(software)}", "|".join(ADES_COLUMNS)]

    for i, o in enumerate(observations):
        perm, prov = o.get("perm_id") or "", o.get("prov_id") or ""
        if perm and prov:
            raise FormatError(f"observation {i}: give at most one of perm_id and prov_id (neither means an object with no designation yet)")
        if perm and not _PERM_ID.match(str(perm)):
            raise FormatError(f"observation {i}: {perm!r} is not a permanent designation")
        if prov and not _PROV_ID.match(str(prov)):
            raise FormatError(f"observation {i}: {prov!r} is not a provisional designation")
        trk = _one_line(o.get("trk_sub") or "")
        if not trk:
            raise FormatError(f"observation {i}: a tracklet label (trk_sub) is required")
        ra, dec = _finite(o.get("ra_deg"), f"observation {i} RA"), _finite(o.get("dec_deg"), f"observation {i} Dec")
        if not (0.0 <= ra < 360.0 and -90.0 <= dec <= 90.0):
            raise FormatError(f"observation {i}: position out of range ({ra}, {dec})")
        rms_ra = _finite(o.get("rms_ra_arcsec"), f"observation {i} rmsRA")
        rms_dec = _finite(o.get("rms_dec_arcsec"), f"observation {i} rmsDec")
        if rms_ra <= 0 or rms_dec <= 0:
            raise FormatError(f"observation {i}: position uncertainties must be positive")
        cat = str(o.get("ast_cat") or "")
        if not cat:
            raise FormatError(f"observation {i}: the astrometric reference catalogue (ast_cat) is required")
        cells = [
            str(perm), str(prov), trk, "CCD", station, ades_obs_time(o["obs_time"]),
            f"{ra:.6f}", f"{dec:+.6f}", f"{rms_ra:.2f}", f"{rms_dec:.2f}", cat,
            _one_line(o.get("notes") or ""), _one_line(o.get("remarks") or ""),
        ]
        if any("|" in c for c in cells):
            raise FormatError(f"observation {i}: a field contains the '|' delimiter")
        lines.append("|".join(cells))
    return "\n".join(lines) + "\n"
