"""Dashboard information architecture: vocabulary, lifecycle lanes, plain-language summaries, and plugin evidence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
from matplotlib import image as mpimg

from core.state import NODE_ORDER, MissionStatus
from dashboard import first_frame, fleet, vocab
from dashboard.plugin_ui import PluginUI, Summary, ViewContext, evidence_for, load_plugin_ui, summary_for
from plugins.asteroid_finder import dashboard as asteroid_view
from plugins.rubin_followup import dashboard as rubin_view
from plugins.transit_finder import dashboard as transit_view

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


def _mission(**over):
    m = {
        "id": "m1", "plugin_name": "asteroid_finder", "status": "OBSERVING", "current_node": "execute_observation",
        "data_source": "telescope", "error": None, "simulated": False, "worker_pid": 1, "worker_host": "h",
        "heartbeat_at": (NOW - timedelta(seconds=3)).isoformat(), "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
        "clock": None, "sim_time": None, "wake_at": None,
        "progress": {"visit": 1, "visits_total": 2, "target": "433 Eros", "frames_done": 3, "frames_total": 10},
    }
    m.update(over)
    return m


def _targets(statuses=("OBSERVED", "PENDING")):
    return [{"position": i, "name": f"T{i + 1}", "status": s, "payload": {}} for i, s in enumerate(statuses)]


def _row(**over):
    return fleet.fleet_row(_mission(**over), _targets(), now=NOW)


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
def test_every_status_and_node_has_a_plain_label_and_a_colour():
    """A new MissionStatus or pipeline node must be named in plain words, or the simple view shows an enum."""
    for status in MissionStatus:
        assert status.value in vocab.STATE_PLAIN and status.value in vocab.STATE_COLORS, status
        assert status.value in vocab.STATE_LOOK, status
    for state in ("ORPHANED", "PAUSED", "IDLE"):  # display states that are not stored statuses
        assert state in vocab.STATE_PLAIN and state in vocab.STATE_COLORS
        assert state in vocab.STATE_LOOK
    for node in NODE_ORDER:
        assert node in vocab.NODE_PLAIN and node in vocab.NODE_TECHNICAL, node


def test_simple_and_technical_vocabularies_differ_for_the_same_state():
    assert vocab.state_label("SLEEP", simple=True) == "Waiting for clear sky"
    assert vocab.state_label("SLEEP", simple=False) == "SLEEPING"
    assert vocab.state_label("SOMETHING_NEW", simple=True) == "Something new"  # unknown states degrade, never raise
    assert vocab.target_status("PENDING", True) == "Not yet observed" and vocab.target_status("PENDING", False) == "PENDING"
    assert vocab.plural(1, "field") == "1 field" and vocab.plural(3, "field") == "3 fields"


# --------------------------------------------------------------------------- #
# States and lifecycle lanes
# --------------------------------------------------------------------------- #
def test_an_unfinished_mission_nobody_owns_is_paused_not_acquiring():
    """After Ctrl-C the lease is released: the status still says OBSERVING, but nothing is observing."""
    paused = _row(heartbeat_at=None)
    assert paused["state"] == "PAUSED" and paused["liveness"] == "idle" and paused["active"] is True
    assert "Ctrl-C" in fleet.describe_state(paused)
    assert _row(status="INIT", heartbeat_at=None)["state"] == "IDLE"  # never started is not "paused"
    assert _row()["state"] == "OBSERVING"  # live heartbeat: the stored status stands


def test_lifecycle_lanes():
    running = _row()
    waiting = _row(status="WAITING", wake_at="2026-10-22T00:00:00+00:00")
    paused = _row(heartbeat_at=None)
    orphaned = _row(heartbeat_at=(NOW - timedelta(minutes=5)).isoformat())
    done = _row(status="COMPLETE", heartbeat_at=None)
    fresh_fail = _row(status="FAILED", error="boom", heartbeat_at=None, updated_at=(NOW - timedelta(hours=1)).isoformat())
    old_fail = _row(status="FAILED", error="boom", heartbeat_at=None, updated_at=(NOW - timedelta(days=3)).isoformat())
    lane = lambda r: vocab.lifecycle(r, NOW)  # noqa: E731
    assert [lane(r) for r in (running, waiting)] == ["in_progress", "in_progress"]
    assert [lane(r) for r in (paused, orphaned, fresh_fail)] == ["attention"] * 3  # a person has to act
    assert [lane(r) for r in (done, old_fail)] == ["completed", "completed"]  # old failures are history

    groups = vocab.group_by_lifecycle([done, running, orphaned], NOW)
    assert list(groups) == ["attention", "in_progress", "completed"]
    assert [r["state"] for r in groups["attention"]] == ["ORPHANED"] and groups["completed"] == [done]


def test_there_is_no_proposed_lane_until_missions_have_an_approval_state():
    assert "proposed" not in vocab.LIFECYCLES and "proposed" not in vocab.LIFECYCLE_TITLES


def test_describe_state_is_a_sentence_for_every_situation():
    assert fleet.describe_state(_row()) == "Taking images of 433 Eros (image 3 of 10)."
    waiting = fleet.describe_state(_row(status="WAITING", wake_at="2026-10-22T00:00:00+00:00"))
    assert waiting.startswith("Waiting for visit 1 of 2, due 2026-10-22 00:00 UTC")
    assert "Taking" not in fleet.describe_state(_row(status="SLEEP"))
    assert fleet.describe_state(_row(status="FAILED", error="boom", heartbeat_at=None)) == "Stopped with an error: boom"
    assert "stopped 5 min ago" in fleet.describe_state(_row(heartbeat_at=(NOW - timedelta(minutes=5)).isoformat()))
    assert fleet.describe_state(_row(status="COMPLETE", heartbeat_at=None)).startswith("Finished: 1 of 2 visits")


def test_titles_name_the_targets_in_simple_mode_and_the_id_in_technical_mode():
    row = fleet.fleet_row(_mission(), _targets(("PENDING",) * 3), now=NOW)
    assert fleet.mission_title(row, "Asteroid search", simple=True) == "Asteroid search: T1, T2 +1 more"
    assert fleet.mission_title(row, "Asteroid search", simple=False) == "Asteroid search `m1`"
    assert fleet.mission_title(fleet.fleet_row(_mission(), [], now=NOW), "Asteroid search", simple=True) == "Asteroid search"


# --------------------------------------------------------------------------- #
# Plain-language summaries (deterministic: computed from stored findings only)
# --------------------------------------------------------------------------- #
def _obs(detections=(), entries=(), non_detection=None, findings_extra=None):
    findings = {"detections": list(detections), **(findings_extra or {})}
    crossref = {"entries": list(entries)}
    if non_detection is not None:
        crossref["non_detection"] = non_detection
    return {"id": 1, "target_name": "F", "findings": findings, "crossref": crossref}


def _ctx(*observations):
    return ViewContext(mission={}, targets=[], observations=list(observations), simple=True)


def test_asteroid_summary_calls_out_unidentified_tracks_without_calling_them_discoveries():
    det = {"id": "T01"}
    known = _obs([det], [{"detection_id": "T01", "classification": "known_object_match"}])
    unid = _obs([det], [{"detection_id": "T01", "classification": "unidentified_candidate"}])
    quiet = _obs()

    s = asteroid_view.summarise(_ctx(known, quiet))
    assert s.verdict == "good" and "1 moving-object candidate in 2 fields" in s.found and "nothing new" in s.meaning
    s = asteroid_view.summarise(_ctx(known, unid))
    assert s.verdict == "attention" and "not a discovery" in s.meaning and "1 unidentified" in s.found
    s = asteroid_view.summarise(_ctx(quiet))
    assert s.verdict == "neutral" and s.found.startswith("No moving-object candidates")

    # a track no catalogue was asked about is "not checked", never "unidentified"
    unchecked = _obs([det], [{"detection_id": "T01", "classification": "unverified"}])
    s = asteroid_view.summarise(_ctx(unchecked))
    assert s.verdict == "neutral" and "0 unidentified" in s.found and "1 not conclusively checked" in s.found
    assert "could not be checked" in s.meaning and "not explained by the catalogues" not in s.meaning
    assert asteroid_view.detection_rows([unchecked], simple=True)[0]["classification"] == "not checked"


def test_transit_summary_reports_the_worst_outcome_and_the_honest_caveat():
    def with_class(kind):
        return _obs([{"id": "D"}], [{"detection_id": "D", "classification": kind}])

    control = _obs(non_detection={"expected": False})
    good = transit_view.summarise(_ctx(with_class("known_transit_confirmed"), control))
    assert good.verdict == "good" and "1 known transit confirmed" in good.found and "control field clean" in good.found
    dip = transit_view.summarise(_ctx(with_class("known_transit_confirmed"), with_class("unexpected_dip")))
    assert dip.verdict == "attention" and "not evidence of a new planet" in dip.meaning
    wrong = transit_view.summarise(_ctx(with_class("unexpected_dip"), with_class("target_identity_mismatch")))
    assert "wrong star" in wrong.found and "may not be the target" in wrong.meaning  # identity outranks the dip
    missed = transit_view.summarise(_ctx(_obs(non_detection={"expected": True, "classification": "expected_transit_not_detected"})))
    assert missed.verdict == "attention" and "expected transit not seen" in missed.found


def test_rubin_summary():
    def alert(kind):
        return _obs([], [{"detection_id": "FU01", "classification": kind}])

    ok = rubin_view.summarise(_ctx(alert("detected_consistent_with_rubin"), alert("detected_consistent_with_rubin")))
    assert ok.verdict == "good" and "2 alerts followed: 2 detected (2 consistent with Rubin)." == ok.found
    gone = rubin_view.summarise(_ctx(alert("detected_consistent_with_rubin"), alert("not_detected_expected_visible")))
    assert gone.verdict == "attention" and "should have been visible" in gone.found
    shallow = rubin_view.summarise(_ctx(alert("not_detected_uninformative")))
    assert shallow.verdict == "neutral" and "1 inconclusive" in shallow.found


def test_summary_for_never_raises_and_ignores_unprocessed_observations():
    def boom(_ctx):
        raise RuntimeError("plugin bug")

    ui = PluginUI("x", "X", "", boom)
    unprocessed = {"id": 1, "findings": None, "crossref": None}  # imaging started, nothing reduced yet
    assert summary_for(ui, _ctx(unprocessed)).found.startswith("Nothing yet")
    generic = summary_for(ui, _ctx(_obs([{"id": 1}, {"id": 2}])))  # the hook raised: generic fallback
    assert generic.found == "2 detections across 1 observation." and generic.verdict == "neutral"

    bad_verdict = PluginUI("x", "X", "", lambda c: Summary("f", "m", "excellent"))
    assert summary_for(bad_verdict, _ctx(_obs())).verdict == "neutral"

    seen = []
    spy = PluginUI("x", "X", "", lambda c: seen.append(len(c.observations)) or None)
    summary_for(spy, _ctx(unprocessed, _obs()))
    assert seen == [1]  # the plugin only sees processed observations


def test_evidence_for_falls_back_to_a_detections_table():
    ui = PluginUI("x", "X", "")
    empty = evidence_for(ui, _ctx())
    assert empty["captions"][0].startswith("Nothing yet")
    table = evidence_for(ui, _ctx(_obs([{"id": "T01", "snr_median": 8}])))
    assert table["tables"][0]["rows"][0]["id"] == "T01"
    assert table["tables"][0]["rows"][0]["classification"] == "unverified"


def test_every_shipped_plugin_view_provides_a_summary_and_json_evidence():
    for name in ("asteroid_finder", "transit_finder", "rubin_followup", "variable_stars"):
        ui = load_plugin_ui(name)
        assert ui.summarise is not None and ui.evidence is not None and ui.label != name, name
        out = evidence_for(ui, _ctx())
        assert isinstance(out, dict)


def test_unreadable_frame_stats_are_ignored(tmp_path):
    (tmp_path / "bad.json").write_text("{not json")
    assert first_frame._read_rows(tmp_path / "bad.json") == []
    assert first_frame._read_rows(tmp_path / "missing.json") == []
    (tmp_path / "odd.json").write_text(json.dumps([{"index": 2}, "x", {"no": "index"}]))
    assert first_frame._read_rows(tmp_path / "odd.json") == [{"index": 2}]


def test_locate_reads_a_picture_that_is_still_waiting_on_the_checks(tmp_path):
    mission = tmp_path / "missions" / "m1"
    mission.mkdir(parents=True)
    mpimg.imsave(mission / "first_frame.png", np.full((8, 8), 0.4), cmap="gray", vmin=0, vmax=1)
    found = first_frame.locate_first_frame(tmp_path, "m1")
    assert found is not None and found.report is None and found.preview.name == "first_frame.png"
    assert first_frame.locate_first_frame(tmp_path, "missing") is None
    assert first_frame.will_show_first_frame({"data_source": "telescope", "config": {"mock_mode": False}})
    assert not first_frame.will_show_first_frame({"data_source": "archive", "config": {"mock_mode": False}})
    assert not first_frame.will_show_first_frame({"data_source": "telescope", "config": {"mock_mode": True}})


# --------------------------------------------------------------------------- #
# Host-star distance in the transit view
# --------------------------------------------------------------------------- #
_DIST = {"archive_pc": 153.242, "gaia_pc": 142.857, "gaia_parallax_mas": 7.0, "gaia_parallax_err_mas": 0.02, "consistent": True}


def test_transit_distance_is_formatted_with_its_sources():
    assert transit_view.distance_text(_DIST) == "153 pc (500 ly)"
    assert transit_view.distance_text({"gaia_pc": 100.0}) == "100 pc (326 ly)"  # Gaia alone is enough
    assert transit_view.distance_text({"archive_pc": None, "gaia_pc": None}) is None
    assert transit_view.distance_text(None) is None  # older missions stored no distance

    detail = transit_view.distance_detail(_DIST)
    assert "153 pc in the NASA Exoplanet Archive" in detail and "143 pc from the Gaia parallax" in detail
    assert "disagree" not in detail
    assert "These disagree" in transit_view.distance_detail({**_DIST, "consistent": False})
    assert transit_view.distance_detail(None) is None


def test_distance_formatter():
    assert vocab.format_distance(153.242) == "153 pc (500 ly)"
    assert vocab.format_distance(None) is None and vocab.format_distance(0) is None and vocab.format_distance(-3.0) is None


def test_a_mission_lists_its_first_frame_and_later_fits(tmp_path):
    from astropy.io import fits

    from dashboard.frames import list_mission_frames, preview_image

    mission = tmp_path / "m1"
    (mission / "frames" / "01_field").mkdir(parents=True)
    mpimg.imsave(mission / "first_frame.png", np.zeros((8, 8)), cmap="gray", vmin=0, vmax=1)
    fits.PrimaryHDU(np.ones((4, 4), dtype=np.float32)).writeto(mission / "frames" / "01_field" / "frame_001.fits")
    fits.PrimaryHDU(np.arange(16, dtype=np.float32).reshape(4, 4)).writeto(mission / "frames" / "01_field" / "frame_002.fits")

    listed = list_mission_frames(mission)
    assert listed[0]["label"] == "First frame" and listed[0]["kind"] == "preview"
    assert [item["path"].name for item in listed[1:]] == ["frame_001.fits", "frame_002.fits"]
    view = preview_image(listed[2]["path"])
    assert view.ndim == 2 and float(view.max()) <= 1.0

