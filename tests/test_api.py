"""The HTTP API is the contract for any frontend.

A confirmed start runs ``python main.py --from-plan``; nothing here talks to the telescope.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.api.app import create_app, ui_url, _want_browser
from db.database import Database


def _client(tmp_path):
    from fastapi.testclient import TestClient

    return TestClient(create_app(data_dir=tmp_path, serve_frontend=False))


def _seed(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "astronomo.sqlite"))
    now = datetime.now(timezone.utc)
    db.create_mission("m_done", "asteroid_finder", True, None)
    tid = db.save_targets("m_done", [{"name": "Field B", "ra_deg": 3.0, "dec_deg": 4.0}])[0]
    db.update_mission("m_done", status="COMPLETE")
    db.update_target(tid, status="OBSERVED")
    db.create_observation(
        "m_done", tid, ended_at=now,
        findings={"summary": "1 candidate", "detections": [{"id": "T01", "points": [], "n_points": 4, "n_frames": 4}]},
        crossref={"summary": "1 unidentified", "entries": [{"detection_id": "T01", "classification": "unidentified_candidate"}]},
    )
    start = now + timedelta(hours=2)
    db.plan_observation("variable_stars", {
        "name": "RR Lyr", "ra_deg": 10.0, "dec_deg": 20.0,
        "window_start": start.isoformat(), "window_end": (start + timedelta(hours=3)).isoformat(),
    })
    db.dispose()


def test_the_api_package_does_not_import_streamlit():
    sys.modules.pop("streamlit", None)
    sys.modules.pop("dashboard.api.app", None)
    sys.modules.pop("dashboard.api.queries", None)
    import dashboard.api.app  # noqa: F401

    assert "streamlit" not in sys.modules


def test_health_and_root_describe_a_headless_api(tmp_path):
    client = _client(tmp_path)
    health = client.get("/api/v1/health")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    root = client.get("/")
    assert root.status_code == 200
    body = root.json()
    assert body["api"] == "/api/v1"
    assert "frontend/dist" in body["note"]


def test_a_built_frontend_is_optional_and_served_from_the_same_process(tmp_path):
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist" / "index.html"
    assert dist.is_file(), "frontend/dist is shipped so python -m dashboard.api has a UI without Node"
    from fastapi.testclient import TestClient

    client = TestClient(create_app(data_dir=tmp_path, serve_frontend=True))
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    html = page.text
    js = re.search(r'src="(/assets/[^"]+\.js)"', html)
    css = re.search(r'href="(/assets/[^"]+\.css)"', html)
    assert js and css, "the shipped index.html must point at a JS bundle and a stylesheet"
    bundled = client.get(js.group(1))
    assert bundled.status_code == 200 and len(bundled.content) > 1000
    style = client.get(css.group(1))
    assert style.status_code == 200 and len(style.content) > 100
    deep = client.get("/missions/m_example")
    assert deep.status_code == 200 and "text/html" in deep.headers["content-type"]
    assert client.get("/api/v1/health").json()["status"] == "ok"


def test_the_ui_url_is_localhost_for_a_loopback_bind():
    assert ui_url("127.0.0.1", 8765) == "http://localhost:8765"
    assert ui_url("0.0.0.0", 9000) == "http://localhost:9000"
    assert ui_url("192.168.1.4", 8765) == "http://192.168.1.4:8765"


def test_the_browser_stays_closed_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert _want_browser(no_browser=False) is False
    monkeypatch.delenv("CI", raising=False)
    assert _want_browser(no_browser=True) is False


def test_tonight_without_a_database_is_empty_not_an_error(tmp_path):
    client = _client(tmp_path)
    r = client.get("/api/v1/tonight")
    assert r.status_code == 200
    body = r.json()
    assert body["have_db"] is False and body["attention"] == [] and "python main.py" in body["empty"]


def test_tonight_and_missions_read_the_seeded_database(tmp_path):
    _seed(tmp_path)
    client = _client(tmp_path)
    tonight = client.get("/api/v1/tonight", params={"wording": "Simple"}).json()
    assert tonight["have_db"] is True
    assert tonight["counts"]["planned"] == 1
    names = [p["target_name"] for p in tonight["plans"]["live"]]
    assert "RR Lyr" in names

    missions = client.get("/api/v1/missions").json()
    assert missions["counts"]["all"] == 1
    assert missions["missions"][0]["id"] == "m_done"
    assert "unidentified" in missions["missions"][0]["outcome"]

    mission = client.get("/api/v1/missions/m_done").json()
    assert mission["mission"]["plugin_label"] == "Asteroid search"
    assert mission["summary"]["verdict"] == "attention"
    assert "not a discovery" in mission["summary"]["meaning"]
    assert mission["evidence"]["tables"][0]["rows"][0]["id"] == "T01"
    llm = mission["activity"]["llm"]
    assert llm["provider"] == "none" and llm["items"] == [] and llm["calls"] == 0
    missing = client.get("/api/v1/missions/nope")
    assert missing.status_code == 404


def test_a_real_start_does_nothing_until_it_is_confirmed(tmp_path, monkeypatch):
    _seed(tmp_path)
    launched = []

    def fake(spec, **kwargs):
        launched.append(spec)
        return {"pid": 99, "plan_id": spec.plan_id, "rehearse": spec.rehearse, "command": spec.command, "log": "", "started_at": "t"}

    monkeypatch.setattr("dashboard.api.queries.launch_plan", fake)
    client = _client(tmp_path)
    plan_id = client.get("/api/v1/tonight").json()["plans"]["live"][0]["id"]

    asking = client.post(f"/api/v1/plans/{plan_id}/start", json={})
    assert asking.status_code == 200
    body = asking.json()
    assert body["ok"] is False and body["needs_confirm"] is True
    assert "real telescope" in body["warning"]
    assert launched == []

    confirmed = client.post(f"/api/v1/plans/{plan_id}/start", json={"confirm": True})
    assert confirmed.status_code == 200
    assert confirmed.json()["ok"] is True and launched[-1].rehearse is False
    assert launched[-1].plan_id == plan_id


def test_cancel_removes_an_open_plan(tmp_path):
    _seed(tmp_path)
    client = _client(tmp_path)
    plan_id = client.get("/api/v1/tonight").json()["plans"]["live"][0]["id"]
    gone = client.post(f"/api/v1/plans/{plan_id}/cancel")
    assert gone.json()["ok"] is True
    assert client.get("/api/v1/tonight").json()["plans"]["live"] == []


def test_settings_round_trip_writes_site_json(tmp_path):
    client = _client(tmp_path)
    before = client.get("/api/v1/settings").json()
    assert "site" in before
    payload = {"name": "Back garden", "lat": 51.5, "lon": -0.12, "elev_m": 20.0, "horizon": [[90, 20]]}
    saved = client.put("/api/v1/settings/site", json=payload)
    if before["site"].get("locked"):
        assert saved.status_code == 400
        return
    assert saved.status_code == 200 and saved.json()["ok"] is True
    after = client.get("/api/v1/settings").json()
    assert after["site"]["name"] == "Back garden"
    assert after["site"]["lat"] == 51.5
    assert (tmp_path / "site.json").is_file()
