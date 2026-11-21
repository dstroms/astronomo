"""Upload-format writers: what they emit, and what they refuse to emit."""

from datetime import datetime, timedelta, timezone

import pytest

from tools import upload_formats as uf
from tools.astrometry import utc_to_bjd_tdb, utc_to_bjd_tdb_many

WHEN = datetime(2026, 10, 21, 3, 15, 30, 250000, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# AAVSO Exoplanet report
# --------------------------------------------------------------------------- #
def _exo(**over):
    kw = dict(
        obs_code="ABC", software="Astronomo test", date_type="BJD_TDB", star_name="WASP-12", exoplanet_name="WASP-12 b",
        exposure_s=10.0, filter_name="CV", measurement_type="Rnflux",
        rows=[(2461000.5, 1.0001, 0.002), (2461000.5007, 0.9871, 0.002), (2461000.5014, 0.9869, None)],
    )
    kw.update(over)
    return uf.aavso_exoplanet_report(**kw)


def test_exoplanet_report_layout():
    lines = _exo(ra_deg=97.636625, dec_deg=29.672278, notes="a note").splitlines()
    header = {ln[1:].split("=", 1)[0]: ln[1:].split("=", 1)[1] for ln in lines if "=" in ln and ln.startswith("#")}
    assert lines[0] == "#TYPE=EXOPLANET"
    assert header["OBSCODE"] == "ABC" and header["DATE_TYPE"] == "BJD_TDB" and header["FILTER"] == "CV"
    assert header["MEASUREMENT_TYPE"] == "Rnflux" and header["EXPOSURE_TIME"] == "10" and header["EPOCH"] == "J2000"
    assert header["DETREND_PARAMETERS"] == "" and header["RA"] == "97.636625" and header["DEC"] == "+29.672278"
    assert "#DATE,DIFF,ERR" in lines
    data = lines[lines.index("#DATE,DIFF,ERR") + 1:]
    assert data[0] == "2461000.50000000,1.000100,0.002000"
    assert data[2].endswith(",na")  # an unknown error is written as na, never as 0


def test_exoplanet_report_columns_follow_the_declared_detrend_parameters():
    text = _exo(detrend_names=["airmass"], rows=[(2461000.5, 1.0, 0.001, 1.2)])
    assert "#DATE,DIFF,ERR,AIRMASS" in text and "#DETREND_PARAMETERS=airmass" in text
    with pytest.raises(uf.FormatError, match="declares 4 columns"):
        _exo(detrend_names=["airmass"], rows=[(2461000.5, 1.0, 0.001)])
    with pytest.raises(uf.FormatError, match="at most four"):
        _exo(detrend_names=list("abcde"), rows=[(1.0, 1.0, 1.0, 1, 1, 1, 1, 1)])


@pytest.mark.parametrize("override, match", [
    ({"obs_code": "TOOLONGCODE"}, "observer code"),
    ({"obs_code": ""}, "observer code"),
    ({"date_type": "UTC"}, "DATE_TYPE"),
    ({"filter_name": "clear!"}, "FILTER"),
    ({"measurement_type": "mag"}, "MEASUREMENT_TYPE"),
    ({"obstype": "CMOS"}, "OBSTYPE"),
    ({"binning": "5x5"}, "BINNING"),
    ({"exposure_s": 0}, "EXPOSURE_TIME"),
    ({"exposure_s": 3600}, "EXPOSURE_TIME"),
    ({"exposure_s": float("nan")}, "not finite"),
    ({"star_name": ""}, "star name"),
    ({"rows": []}, "at least one"),
    ({"rows": [(float("inf"), 1.0, 0.1)]}, "not finite"),
    ({"delim": "x"}, "DELIM"),
])
def test_exoplanet_report_refuses_what_the_spec_forbids(override, match):
    with pytest.raises(uf.FormatError, match=match):
        _exo(**override)


def test_header_values_cannot_inject_extra_lines():
    text = _exo(notes="first line\n#TYPE=EVIL", star_name="X\nY")
    assert sum(ln.startswith("#TYPE=") for ln in text.splitlines()) == 1  # the injected text stays inside #NOTES
    assert "#NOTES=first line #TYPE=EVIL" in text
    assert "#STAR_NAME=X Y" in text


# --------------------------------------------------------------------------- #
# AAVSO Extended
# --------------------------------------------------------------------------- #
def _ext(rows=None, **over):
    row = {"name": "AT 2026snc", "jd": 2461000.12345, "mag": 15.684, "merr": 0.03, "filt": "CV", "trans": False,
           "mtype": "STD", "cname": "ENSEMBLE", "chart": "Gaia DR3 G", "notes": "zero point, scatter"}
    return uf.aavso_extended_report(obs_code="ABC", software="Astronomo", rows=rows or [row], **over)


def test_extended_report_layout_and_na_for_missing_optionals():
    lines = _ext().splitlines()
    assert lines[0] == "#TYPE=EXTENDED" and "#DATE=JD" in lines and "#OBSTYPE=CCD" in lines
    assert "#NAME,DATE,MAG,MERR,FILT,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,AMASS,GROUP,CHART,NOTES" in lines
    row = lines[-1].split(",")
    assert row[:7] == ["AT 2026snc", "2461000.12345", "15.684", "0.030", "CV", "NO", "STD"]
    assert row[8:13] == ["na"] * 5  # CMAG, KNAME, KMAG, AMASS, GROUP
    assert row[13] == "Gaia DR3 G"


def test_extended_report_notes_cannot_break_the_columns():
    text = _ext([{"name": "S", "jd": 1.0, "mag": 12.0, "merr": None, "filt": "V", "notes": "a,b,c"}])
    assert len(text.splitlines()[-1].split(",")) == 15  # the delimiter inside NOTES was replaced


def test_extended_report_fainter_than_limit():
    text = _ext([{"name": "S", "jd": 2461000.1, "mag": ("<", 18.6), "merr": None, "filt": "CV"}])
    assert text.splitlines()[-1].split(",")[2] == "<18.600"
    with pytest.raises(uf.FormatError, match="only '<'"):
        _ext([{"name": "S", "jd": 1.0, "mag": (">", 18.6), "merr": None, "filt": "CV"}])


@pytest.mark.parametrize("row_over, match", [
    ({"filt": "XX"}, "filter"),
    ({"name": ""}, "no star name"),
    ({"mag": float("nan")}, "not finite"),
    ({"mag": 123456.789}, "8 characters"),
    ({"mtype": "DIF", "cname": None}, "comparison star"),
    ({"mtype": "XYZ"}, "MTYPE"),
    ({"name": "A#B"}, "'#'"),
])
def test_extended_report_refuses_bad_rows(row_over, match):
    row = {"name": "S", "jd": 2461000.1, "mag": 12.0, "merr": 0.01, "filt": "V", **row_over}
    with pytest.raises(uf.FormatError, match=match):
        _ext([row])


def test_extended_report_header_checks():
    with pytest.raises(uf.FormatError, match="observer code"):
        uf.aavso_extended_report(obs_code="not valid", software="x", rows=[{"name": "S", "jd": 1.0, "mag": 1.0, "filt": "V"}])
    with pytest.raises(uf.FormatError, match="at least one"):
        uf.aavso_extended_report(obs_code="ABC", software="x", rows=[])


# --------------------------------------------------------------------------- #
# ADES
# --------------------------------------------------------------------------- #
def _obs(i=0, **over):
    o = {"perm_id": "433", "trk_sub": "AF00T01", "obs_time": WHEN + timedelta(seconds=10 * i),
         "ra_deg": 150.123456, "dec_deg": -11.5, "rms_ra_arcsec": 0.4, "rms_dec_arcsec": 0.4, "ast_cat": "Gaia3"}
    o.update(over)
    return o


def _ades(observations=None, **over):
    kw = dict(station="Z99", submitter="A. Observer", observations=observations or [_obs(0), _obs(1)])
    kw.update(over)
    return uf.ades_psv(**kw)


def test_ades_layout():
    lines = _ades(telescope={"design": "refractor", "aperture": 0.05, "fRatio": 5.0}).splitlines()
    assert lines[0] == "# version=2017" and "! mpcCode Z99" in lines and "! detector CCD" in lines
    assert "! aperture 0.05" in lines
    header = lines.index("permID|provID|trkSub|mode|stn|obsTime|ra|dec|rmsRA|rmsDec|astCat|notes|remarks")
    first = lines[header + 1].split("|")
    assert first[:6] == ["433", "", "AF00T01", "CCD", "Z99", "2026-10-21T03:15:30.250Z"]
    assert first[6:11] == ["150.123456", "-11.500000", "0.40", "0.40", "Gaia3"]
    assert len(lines) == header + 3 and all(len(ln.split("|")) == 13 for ln in lines[header:])


def test_ades_object_without_a_designation_is_identified_by_trksub_alone():
    row = _ades([_obs(perm_id=None, trk_sub="P22abcd")]).splitlines()[-1].split("|")
    assert row[0] == "" and row[1] == "" and row[2] == "P22abcd"


def test_ades_accepts_provisional_designations():
    row = _ades([_obs(perm_id=None, prov_id="2019 AB12")]).splitlines()[-1].split("|")
    assert row[1] == "2019 AB12"


@pytest.mark.parametrize("over, match", [
    ({"perm_id": "433", "prov_id": "2019 AB12"}, "at most one"),
    ({"perm_id": "Eros"}, "permanent designation"),
    ({"perm_id": None, "prov_id": "19AB"}, "provisional"),
    ({"trk_sub": ""}, "tracklet"),
    ({"ra_deg": 360.0}, "out of range"),
    ({"dec_deg": 91.0}, "out of range"),
    ({"rms_ra_arcsec": 0.0}, "positive"),
    ({"ast_cat": ""}, "catalogue"),
    ({"obs_time": datetime(2026, 10, 21, 3, 0, 0)}, "timezone"),
    ({"ra_deg": float("nan")}, "not finite"),
])
def test_ades_refuses_bad_observations(over, match):
    with pytest.raises(uf.FormatError, match=match):
        _ades([_obs(**over)])


def test_ades_header_checks_and_no_magnitudes():
    with pytest.raises(uf.FormatError, match="station"):
        _ades(station="12")
    with pytest.raises(uf.FormatError, match="submitter"):
        _ades(submitter="a|b")
    with pytest.raises(uf.FormatError, match="at least one"):
        uf.ades_psv(station="Z99", submitter="x", observations=[])
    assert "mag" not in _ades().lower().split("permid")[1]  # magnitudes are deliberately never written


def test_ades_observation_times_are_utc():
    est = timezone(timedelta(hours=-5))
    assert uf.ades_obs_time(WHEN.astimezone(est)) == "2026-10-21T03:15:30.250Z"


# --------------------------------------------------------------------------- #
# BJD_TDB conversion used for the transit upload
# --------------------------------------------------------------------------- #
def test_the_vectorised_bjd_conversion_matches_the_scalar_one():
    times = [WHEN, WHEN + timedelta(hours=1), WHEN + timedelta(hours=2)]
    many = utc_to_bjd_tdb_many(times, 97.6366, 29.6723, 40.0, -105.0, 1600.0)
    one = [utc_to_bjd_tdb(t, 97.6366, 29.6723, 40.0, -105.0, 1600.0) for t in times]
    assert list(many) == pytest.approx(one, abs=1e-9)
    assert many[1] - many[0] == pytest.approx(1.0 / 24.0, abs=1e-4)  # an hour apart, to within the changing light-time
