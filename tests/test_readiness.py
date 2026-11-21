"""Dew, battery and free space. An unattended start does not guess when a reading is missing."""

from pathlib import Path

from core.readiness import battery_reading, dew_point_c, dew_reading, disk_reading, start_blockers


def test_dew_point_rises_with_humidity():
    assert dew_point_c(10.0, 20.0) < dew_point_c(10.0, 90.0)


def test_an_unknown_battery_blocks_only_an_unattended_start():
    readings = [battery_reading(None, 20.0), dew_reading(8.0, 40.0, 2.0), disk_reading(Path("."), 1)]
    assert start_blockers(readings, unattended=False) == []
    blocked = start_blockers(readings, unattended=True)
    assert len(blocked) == 1 and "battery" in blocked[0]


def test_a_known_bad_reading_blocks_a_confirmed_start_too(tmp_path, monkeypatch):
    low = battery_reading(5.0, 20.0)
    assert low.ok is False and low.known is True
    assert start_blockers([low], unattended=False) == [low.detail]

    def full(_path):
        raise OSError("full")

    monkeypatch.setattr("core.readiness.shutil.disk_usage", full)
    missing = disk_reading(tmp_path, 1)
    assert missing.known is False
    assert start_blockers([missing], unattended=False)  # free space is not optional, even with a person watching
