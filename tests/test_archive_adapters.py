"""The ZTF archive adapter (via IRSA) against fake servers: no network.

The IRSA services are replaced by a stub ``http_get``; the data are small synthetic star fields.
"""

from __future__ import annotations

import io
from datetime import timezone

import numpy as np
import pytest
from astropy.io import fits
from astropy.time import Time

from core.archive import ArchiveError
from plugins.asteroid_finder import ztf

# --------------------------------------------------------------------------- #
# Synthetic star fields
# --------------------------------------------------------------------------- #
def _stars(rng, shape, n=45, margin=25, min_sep=20.0):
    ny, nx = shape
    pts: list[tuple[float, float, float]] = []
    for _ in range(20000):
        if len(pts) == n:
            return pts
        x, y = rng.uniform(margin, nx - margin), rng.uniform(margin, ny - margin)
        if all((x - a) ** 2 + (y - b) ** 2 > min_sep**2 for a, b, _ in pts):
            pts.append((x, y, rng.uniform(4000, 20000)))
    raise AssertionError(f"cannot place {n} stars {min_sep} px apart in {shape} (got {len(pts)}): relax the fixture")


def _render(shape, stars, sigma=1.7):
    ny, nx = shape
    yy, xx = np.mgrid[:ny, :nx]
    img = np.zeros(shape)
    for x, y, a in stars:
        img += a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))
    return img


def _tan_header(shape, crval=(307.67, 6.36), scale_arcsec=0.736, crpix=None):
    ny, nx = shape
    hdr = fits.Header()
    hdr["CTYPE1"], hdr["CTYPE2"] = "RA---TAN", "DEC--TAN"
    hdr["CRVAL1"], hdr["CRVAL2"] = crval
    hdr["CRPIX1"], hdr["CRPIX2"] = crpix or (nx / 2.0, ny / 2.0)
    hdr["CD1_1"], hdr["CD1_2"], hdr["CD2_1"], hdr["CD2_2"] = -scale_arcsec / 3600.0, 0.0, 0.0, scale_arcsec / 3600.0
    hdr["CUNIT1"] = hdr["CUNIT2"] = "deg"
    return hdr


def _fits_bytes(data, hdr=None) -> bytes:
    buf = io.BytesIO()
    fits.PrimaryHDU(np.asarray(data, dtype=np.float32), hdr).writeto(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# ZTF: MOST table, visit grouping, cutouts
# --------------------------------------------------------------------------- #
_COLS = ["ra_obj", "dec_obj", "filefracday", "field", "ccdid", "qid", "filtercode", "obsjd", "imgtypecode", "vmag"]


def _most_text(rows: list[dict], header="\\fixlen = T\n\\RowsRetrieved = {n}\n") -> str:
    width = {c: max(len(c), *(len(str(r[c])) for r in rows)) + 2 for c in _COLS}
    types = dict(zip(_COLS, ["double", "double", "long", "int", "int", "int", "char", "double", "char", "double"]))
    lines = [header.format(n=len(rows)).rstrip("\n")]
    lines.append("|" + "|".join(c.center(width[c]) for c in _COLS) + "|")
    lines.append("|" + "|".join(types[c].center(width[c]) for c in _COLS) + "|")
    for r in rows:
        lines.append(" " + " ".join(str(r[c]).rjust(width[c]) for c in _COLS) + " ")  # bars become blanks, columns stay aligned
    return "\n".join(lines) + "\n"


def _row(jd, field=520, ccd=7, q=1, fc="zr", ffd=None, img="o"):
    t = Time(jd, format="jd", scale="utc").to_datetime(timezone=timezone.utc)
    return {
        "ra_obj": 130.5, "dec_obj": 12.25, "filefracday": ffd or f"{t:%Y%m%d}{int((t.hour * 3600 + t.minute * 60 + t.second) / 86400 * 1e6):06d}",
        "field": field, "ccdid": ccd, "qid": q, "filtercode": fc, "obsjd": jd, "imgtypecode": img, "vmag": 16.3,
    }


JD0 = 2458503.8  # 2019-01-20 ~07:12 UTC


def test_parse_most_table_sorts_by_time_and_keeps_only_science_exposures():
    rows = [_row(JD0 + 0.01), _row(JD0), _row(JD0 + 0.02, img="b"), _row(JD0 + 0.03, ffd="20190120999999")]
    exposures = ztf.parse_most_table(_most_text(rows))
    assert len(exposures) == 3  # the bias frame is dropped
    assert [e.t_iso for e in exposures] == sorted(e.t_iso for e in exposures)
    assert exposures[0].field == 520 and exposures[0].fc == "zr" and exposures[0].vmag == pytest.approx(16.3)
    assert exposures[0].t_iso.startswith("2019-01-20T07")  # OBSJD converted to UTC


def test_parse_most_table_reports_a_service_error_instead_of_returning_nothing():
    with pytest.raises(ArchiveError, match="no table"):
        ztf.parse_most_table("\\fixlen = T\nERROR: no observations match your query\n")
    with pytest.raises(ArchiveError, match="lacks columns"):
        ztf.parse_most_table("|ra_obj|dec_obj|\n|double|double|\n 1.0 2.0 \n")


def test_visits_split_on_gaps_and_on_a_change_of_field_ccd_quadrant_or_filter():
    # Burst of 4 in field 520/c07/q1/r, then the same chain 3 days later, then a pair in another quadrant.
    rows = [_row(JD0 + i * 0.0004) for i in range(4)] + [_row(JD0 + 3 + i * 0.0004) for i in range(3)]
    rows += [_row(JD0 + 0.001, q=2), _row(JD0 + 0.0015, q=2)]
    visits = ztf.group_visits(ztf.parse_most_table(_most_text(rows)))
    assert [len(v) for v in visits] == [4, 2, 3]  # time-ordered by first exposure
    assert {(e.field, e.ccd, e.q, e.fc) for e in visits[0]} == {(520, 7, 1, "zr")}
    assert visits[1][0].q == 2


def test_ibe_path_is_the_archive_directory_layout():
    e = ztf.Exposure("2019-01-20T07:19:00+00:00", "20190120305475", 520, 7, 1, "zr", 1.0, 2.0, None)
    assert ztf.ibe_path(e) == "2019/0120/305475/ztf_20190120305475_000520_zr_c07_o_q1_sciimg.fits"


def test_tan_refit_of_a_tan_header_has_negligible_residual():
    hdr = _tan_header((120, 120), crval=(130.5, 12.25), scale_arcsec=1.01)
    tan, err = ztf.fit_tan_wcs(hdr, (120, 120))
    assert err < 0.01  # arcsec
    assert tan.wcs.ctype[0].startswith("RA---TAN")


class FakeIRSA:
    """MOST + IBE: serves a table for MOST, a synthetic cutout per science image, and records every call."""

    def __init__(self, rows, bad_ffd=(), missing_ffd=()):
        self.rows, self.bad, self.missing = rows, set(bad_ffd), set(missing_ffd)
        self.calls: list[str] = []
        self.rng = np.random.default_rng(3)

    def __call__(self, url, *, params=None, timeout=0, stream_to=None, **_):
        self.calls.append(url)
        if "nph-most" in url:
            return _most_text(self.rows).encode()
        ffd = url.split("/ztf_")[1].split("_")[0]
        if ffd in self.missing:
            return b"<html>not found</html>"
        row = next(r for r in self.rows if r["filefracday"] == ffd)
        img = self._image(row, self.rows.index(row))
        hdr = _tan_header(img.shape, crval=(130.5, 12.25), scale_arcsec=1.01)
        hdr["OBSJD"] = row["obsjd"]
        hdr["EXPTIME"] = 30.0
        hdr["INFOBITS"] = 2**25 if ffd in self.bad else 0
        return _fits_bytes(img, hdr)

    def _image(self, row, index):  # subclasses draw a sky here
        return self.rng.normal(100, 5, (72, 72))


def _ztf(tmp_path, rows, **kw):
    fake = FakeIRSA(rows, **{k: kw.pop(k) for k in ("bad_ffd", "missing_ffd") if k in kw})
    return ztf.ZTFArchive(tmp_path, http_get_fn=fake, **kw), fake


def _burst(n, jd=JD0, base="2019012030", **kw):
    # distinct file-fraction-days (pass another ``base`` per burst), 0.0004 d (~35 s) apart like ZTF's 30 s exposures
    return [_row(jd + i * 0.0004, ffd=f"{base}{i:04d}", **kw) for i in range(n)]


def test_targets_for_builds_one_target_per_usable_visit_in_time_order(tmp_path):
    archive, fake = _ztf(tmp_path, _burst(5) + _burst(2, jd=JD0 + 1, base="2019012130") + _burst(4, jd=JD0 + 20, base="2019020930"))
    targets, summary = archive.targets_for("8191", "2019-01-10", "2019-02-10")
    assert [t["not_before"][:10] for t in targets] == ["2019-01-20", "2019-02-09"]  # the pair is not usable
    assert summary["n_exposures"] == 11 and summary["n_visits_total"] == 3
    assert targets[0]["visit"]["n_used"] == 5 and targets[0]["survey"]["n_usable_visits"] == 2
    assert targets[0]["designation"] == "8191" and targets[0]["priority"] > targets[1]["priority"]
    # The survey query is cached: a second archive object (a "resumed process") does not call MOST again.
    again = ztf.ZTFArchive(tmp_path, http_get_fn=fake)
    again.targets_for("8191", "2019-01-10", "2019-02-10")
    assert sum("nph-most" in c for c in fake.calls) == 1


def test_survey_refuses_odd_object_names(tmp_path):
    archive, _ = _ztf(tmp_path, _burst(3))
    with pytest.raises(ArchiveError, match="odd object name"):
        archive.survey("8191; rm -rf /", "2019-01-10", "2019-02-10")


def test_acquire_writes_time_stamped_wcs_frames_skips_bad_ones_and_caches(tmp_path):
    rows = _burst(6)
    archive, fake = _ztf(tmp_path, rows, bad_ffd={rows[2]["filefracday"]}, missing_ffd={rows[4]["filefracday"]})
    target = archive.targets_for("8191", "2019-01-10", "2019-02-10")[0][0]
    seen = []
    batch = archive.acquire(target, tmp_path / "out", on_frame=lambda i, n, t: seen.append((i, n, t)))

    assert len(batch.paths) == 4  # one INFOBITS-flagged, one missing cutout
    skipped = {s["frame"]: s["reason"] for s in batch.meta["skipped"]}
    assert set(skipped) == {rows[2]["filefracday"], rows[4]["filefracday"]}
    assert "INFOBITS" in skipped[rows[2]["filefracday"]] and "no FITS cutout" in skipped[rows[4]["filefracday"]]
    # on_frame is announced before every requested frame, in survey time order, then once with (total, total, None).
    assert [s[0] for s in seen] == list(range(7)) and seen[0][1] == 6 and seen[-1][2] is None
    assert [s[2] for s in seen[:-1]] == sorted(s[2] for s in seen[:-1])
    # Each frame carries the exposure's own time (from OBSJD), a TAN WCS and the provenance keywords.
    h0 = fits.getheader(batch.paths[0])
    assert h0["DATE-OBS"].startswith("2019-01-20T07:1") and h0["CTYPE1"] == "RA---TAN" and h0["INSTRUME"] == "ZTF"
    assert h0["EXPTIME"] == 30.0 and h0["TANFITER"] < 0.01 and h0["FILTER"] == "ZTF r"
    assert batch.plan["count"] == 4 and batch.plan["pixel_scale_arcsec"] == pytest.approx(1.01)
    assert batch.meta["credit"] == ztf.CREDIT and batch.meta["observatory_code"] == "I41"
    assert batch.meta["cadence_s"] == pytest.approx(34.56, abs=1.0)

    n_ibe = sum("ibe" in c for c in fake.calls)
    archive.acquire(target, tmp_path / "out2")  # cutouts are cached on disk
    assert sum("ibe" in c for c in fake.calls) == n_ibe + 1  # only the missing (never cached) one is requested again


def test_acquire_fails_loudly_when_too_few_cutouts_survive(tmp_path):
    rows = _burst(4)
    archive, _ = _ztf(tmp_path, rows, bad_ffd={r["filefracday"] for r in rows[1:]})
    target = archive.targets_for("8191", "2019-01-10", "2019-02-10")[0][0]
    with pytest.raises(ArchiveError, match="only 1 usable ZTF cutout"):
        archive.acquire(target, tmp_path / "out")
