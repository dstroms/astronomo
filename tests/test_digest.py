"""The data firewall: bulk data must be rejected, concise digests must pass."""

import json

import numpy as np
import pytest

from core.digest import (
    LLMPayloadError,
    PayloadLimits,
    assert_text_safe,
    compact,
    default_summary,
    find_violations,
    looks_like_data_dump,
    render_payload,
    round_floats,
    scalars_only,
    ungrounded_numbers,
)

RNG = np.random.default_rng(0)


def test_small_rounded_digest_passes_and_is_compact_json():
    digest = {"id": "T01", "ra_deg": 150.1234, "snr": 28.7, "quality": "good", "match": None, "ok": True}
    text = render_payload(digest)
    assert json.loads(text) == digest
    assert " " not in text.replace("good", "")  # compact separators: no wasted tokens


def test_numpy_scalars_are_accepted_arrays_are_not():
    assert json.loads(render_payload({"x": np.float64(1.5), "n": np.int64(3)})) == {"x": 1.5, "n": 3}
    with pytest.raises(LLMPayloadError, match="ndarray.*not plain JSON.*reduce it"):
        render_payload({"pixels": RNG.normal(size=(64, 64))})


@pytest.mark.parametrize(
    "bulk",
    [
        RNG.normal(size=500).tolist(),  # what to_jsonable() turns an array into
        RNG.normal(size=(150, 2)).tolist(),  # e.g. findings["astrometry"]["reference_stars"]
        {"nested": {"deep": [list(range(40))]}},
    ],
)
def test_array_like_lists_are_rejected(bulk):
    with pytest.raises(LLMPayloadError, match="array-like|numbers"):
        render_payload({"data": bulk})


def test_many_small_records_still_hit_the_total_number_budget():
    table = [{"a": float(i), "b": float(i), "c": float(i), "d": float(i)} for i in range(80)]  # 320 numbers
    with pytest.raises(LLMPayloadError, match="holds 3\\d\\d numbers"):
        render_payload({"rows": table})


def test_oversize_payload_is_rejected_not_truncated():
    with pytest.raises(LLMPayloadError, match="chars"):
        render_payload({"text": ["word " * 100] * 30}, PayloadLimits(max_chars=500))


@pytest.mark.parametrize(
    "bad,match",
    [
        ({"x": float("nan")}, "non-finite"),
        ({"x": b"\x00\x01"}, "bytes"),
        ({"x": object()}, "object"),
        ({"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}, "deeper"),
        ({1: "non-string key"}, "non-string key"),
    ],
)
def test_other_non_digest_shapes_are_rejected(bad, match):
    with pytest.raises(LLMPayloadError, match=match):
        render_payload(bad)


def test_strings_that_are_data_dumps_are_rejected():
    header = "SIMPLE  =                    T\nBITPIX  =                  -32\nNAXIS   =                    2"
    assert looks_like_data_dump(header)
    assert looks_like_data_dump(" ".join(f"{v:.3f}" for v in RNG.normal(size=60)))
    assert looks_like_data_dump("A" * 300)  # base64-ish blob
    assert looks_like_data_dump("A candidate moving at 0.25 px/s, SNR 28, 4 of 4 detections.") is None
    for text in (header, "A" * 300):
        with pytest.raises(LLMPayloadError):
            render_payload({"note": text})


def test_assert_text_safe_guards_the_final_assembled_prompt():
    assert_text_safe("short prompt", 100)
    with pytest.raises(LLMPayloadError, match="limit"):
        assert_text_safe("x " * 200, 100)
    with pytest.raises(LLMPayloadError, match="run of bare numbers"):
        assert_text_safe(",".join(str(i) for i in range(100)), 10_000)


def test_compact_turns_bulk_into_a_few_statistics_that_pass():
    heavy = {
        "pixels": RNG.normal(100, 5, size=(64, 64)),
        "reference_stars": RNG.uniform(0, 1000, size=(150, 2)).tolist(),
        "points": [{"frame": i, "x": float(i), "y": float(2 * i)} for i in range(40)],
        "name": "word " * 200,  # long prose is truncated...
        "blob": "z" * 900,  # ...an opaque dump is replaced outright
        "wcs": {"crval": [150.0, 11.0]},
    }
    small = compact(heavy)
    assert small["blob"].startswith("<omitted")
    assert small["pixels"].keys() == {"n", "min", "median", "max"} and small["pixels"]["n"] == 4096
    assert small["reference_stars"]["n"] == 300
    assert small["points"]["n_total"] == 40 and len(small["points"]["first"]) == 8
    assert len(small["name"]) <= 300
    assert find_violations(small) == []
    assert len(json.dumps(small)) < 1500


def test_round_floats_and_scalars_only():
    assert round_floats({"a": 123.456789, "b": [0.000123456, float("inf")]}, 3) == {"a": 123.0, "b": [0.000123, None]}
    kept = scalars_only(
        {"name": "Eros", "ra": 150.123456789, "sim": {"seed": 1}, "xs": [1, 2], "file": "frame_01.fits", "n": 3},
        exclude=("n",),
    )
    assert kept == {"name": "Eros", "ra": 150.12}


def test_default_summary_is_default_deny_for_heavy_findings():
    findings = {
        "summary": "1 candidate",
        "n_frames": 4,
        "detections": [{"id": "T01", "points": [{"x": 1.0}] * 50}],
        "astrometry": {"reference_stars": RNG.normal(size=(150, 2)).tolist()},
        "frame_paths": ["/data/f1.fits"] * 4,
        "figures": ["/data/fig.png"],
    }
    summary = default_summary(findings, {"summary": "ok", "entries": [{}] * 3}, {"name": "T", "ra_deg": 1.0, "db_id": 9})
    assert summary["findings"] == {"n_frames": 4, "n_detections": 1, "n_astrometry": 1, "n_frame_paths": 4, "n_figures": 1}
    assert summary["n_crossref_entries"] == 3 and summary["target"] == {"name": "T", "ra_deg": 1.0}
    text = render_payload(summary)
    assert "fits" not in text and "reference_stars" not in text
    # ...and the raw findings would have been refused:
    with pytest.raises(LLMPayloadError):
        render_payload(findings)


def test_ungrounded_numbers_flags_invented_figures_but_allows_rounding():
    payload = json.dumps({"snr": 28.7, "rate_arcsec_per_min": 15.123, "n_tracks": 2, "epoch": "2026-10-20T22:00:00"})
    assert ungrounded_numbers("The track has SNR 28.7 and moves 15.1 arcsec/min; roughly 29 and 15.", payload) == []
    assert ungrounded_numbers("One track at SNR 150 moving 15.1 arcsec/min.", payload) == ["150"]
    assert ungrounded_numbers("Reference frame J2000, Gaia DR3, Table1.", payload) == []  # digits inside words
