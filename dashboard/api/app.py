"""FastAPI application: the HTTP contract for any Astronomo frontend.

``core/`` does not import this package.  This process is the HTTP contract and, when
``frontend/dist`` is present, the bundled React UI.
"""

from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from core import site as core_site
from . import queries
from dashboard.frames import list_mission_frames, preview_image
from dashboard.opportunities import closed_window_sentence
from db.database import Database

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PORT = 8765
_LOOPBACK = {"127.0.0.1", "0.0.0.0", "::", "::1", "localhost"}


def ui_url(host: str, port: int) -> str:
    """The address to open in a browser. Loopback binds are advertised as localhost."""
    shown = "localhost" if host in _LOOPBACK else host
    return f"http://{shown}:{port}"


def port_in_use(port: int) -> bool:
    """True when anything already answers on this port, on IPv4 or IPv6 loopback.

    Binding next to a wildcard listener succeeds on macOS, but `localhost` then reaches the other
    program, so the check is a connect and not a bind.
    """
    import errno
    import socket

    # A refused connection means free. A connection that is accepted, or that stalls because a
    # listener's queue is full, means something is there.
    busy = {0, errno.EAGAIN, errno.ETIMEDOUT, errno.EWOULDBLOCK}
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                if probe.connect_ex((address, port)) in busy:
                    return True
        except (OSError, socket.timeout):
            continue
    return False


def choose_port(port: int, *, explicit: bool, tries: int = 20) -> int:
    """The port to serve on. A port the user asked for is used or refused; the default moves up to the next free one."""
    if not port_in_use(port):
        return port
    if explicit:
        raise SystemExit(
            f"Port {port} is already in use by another program. Stop it, or choose another port "
            "with --port or ASTRONOMO_API_PORT."
        )
    for candidate in range(port + 1, port + 1 + tries):
        if not port_in_use(candidate):
            return candidate
    raise SystemExit(f"Ports {port} to {port + tries} are all in use. Choose one with --port or ASTRONOMO_API_PORT.")


def _open_browser(url: str) -> None:
    import threading
    import webbrowser

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()


def _want_browser(*, no_browser: bool) -> bool:
    if no_browser:
        return False
    flag = os.environ.get("ASTRONOMO_API_NO_BROWSER", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return False
    if os.environ.get("CI"):
        return False
    return bool(sys.stdin.isatty())


def _cors_origins() -> list[str]:
    raw = os.environ.get("ASTRONOMO_API_CORS", "*").strip()
    return ["*"] if raw == "*" else [p.strip() for p in raw.split(",") if p.strip()]


class SiteBody(BaseModel):
    name: str
    lat: float
    lon: float
    elev_m: float = 0.0
    horizon: list[list[float]] = Field(default_factory=list)


class SearchBody(BaseModel):
    query: str
    count: int = 5


class PlanBody(BaseModel):
    plugin: str
    opportunity_id: str
    window_index: int = 0


class ConfirmBody(BaseModel):
    confirm: bool = False


def _db(settings) -> Database | None:
    path = Path(settings.data_dir) / "astronomo.sqlite"
    if not path.is_file():
        return None
    return Database(str(path))


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "simple"}


def create_app(*, data_dir: Path | None = None, serve_frontend: bool | None = None) -> FastAPI:
    """Build the API.  ``data_dir`` is for tests; otherwise ``ASTRONOMO_DATA_DIR`` / the default.

    If ``frontend/dist`` exists, this process serves that React build at ``/`` unless
    ``serve_frontend=False`` (tests).  The build is shipped in the repository.
    """
    app = FastAPI(
        title="Astronomo",
        description=(
            "Headless HTTP API for Astronomo. Missions, plans, opportunities and settings. "
            "A confirmed start runs `python main.py --from-plan`; nothing here talks to the telescope. "
            "See docs/api.md."
        ),
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.data_dir = Path(data_dir) if data_dir is not None else None
    app.state.opp_cache: dict[str, Any] = {}
    app.state.forecasts: dict[str, Any] = {}

    def settings():
        if app.state.data_dir is not None:
            return config.get_settings(data_dir=app.state.data_dir)
        return config.get_settings()

    def flags(request: Request) -> tuple[bool, bool]:
        simple = _bool(request.query_params.get("simple"), True)
        if request.query_params.get("wording") == "Technical":
            simple = False
        if request.query_params.get("wording") == "Simple":
            simple = True
        hour12 = request.query_params.get("clock", "12-hour") != "24-hour"
        return simple, hour12

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "astronomo"}

    @app.get("/api/v1/shell")
    def shell(request: Request) -> dict:
        s = settings()
        db = _db(s)
        n = queries.attention_count(db, s.lease_timeout_s) if db is not None else 0
        return queries.shell_payload(s, have_db=db is not None, attention_count=n)

    @app.get("/api/v1/settings")
    def get_settings() -> dict:
        s = settings()
        return queries.jsonable({
            "data_dir": str(s.data_dir),
            "db_path": str(Path(s.data_dir) / "astronomo.sqlite"),
            "site": {
                "name": s.site_name,
                "lat": s.site_latitude_deg,
                "lon": s.site_longitude_deg,
                "elev_m": s.site_elevation_m,
                "horizon": list(s.site_horizon or ()),
                "source": s.site_source,
                "locked": s.site_source == "environment",
            },
        })

    @app.put("/api/v1/settings/site")
    def put_site(body: SiteBody) -> dict:
        s = settings()
        if s.site_source == "environment":
            raise HTTPException(400, "The observing site is set from the environment and cannot be edited here.")
        try:
            saved = core_site.save_site_file(s.data_dir, body.name, body.lat, body.lon, body.elev_m, body.horizon)
        except core_site.SiteError as exc:
            raise HTTPException(400, str(exc)) from exc
        app.state.opp_cache.clear()
        return {"ok": True, "site": saved}

    @app.post("/api/v1/settings/site/search")
    def search_site(body: SearchBody) -> dict:
        try:
            places = core_site.geocode(body.query, count=body.count)
        except core_site.SiteError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "places": [
                {"name": p.name, "lat": p.lat, "lon": p.lon, "elev_m": p.elev_m, "detail": p.detail, "label": p.label}
                for p in places
            ]
        }

    @app.get("/api/v1/tonight")
    def tonight(request: Request) -> dict:
        s = settings()
        simple, hour12 = flags(request)
        return queries.tonight_payload(_db(s), s, simple=simple, hour12=hour12)

    @app.get("/api/v1/missions")
    def missions(request: Request) -> dict:
        s = settings()
        simple, _hour12 = flags(request)
        return queries.missions_payload(_db(s), s, simple=simple)

    @app.get("/api/v1/missions/{mission_id}")
    def mission(mission_id: str, request: Request) -> dict:
        s = settings()
        db = _db(s)
        if db is None:
            raise HTTPException(404, f"There is no database at {s.data_dir / 'astronomo.sqlite'}.")
        simple, _hour12 = flags(request)
        payload = queries.mission_payload(db, s, mission_id, simple=simple)
        if payload is None:
            raise HTTPException(404, f"There is no mission `{mission_id}` in this database.")
        payload["switcher"] = queries.switcher_payload(db, s, simple=simple)
        return payload

    @app.get("/api/v1/missions/{mission_id}/first-frame")
    def first_frame_image(mission_id: str) -> FileResponse:
        s = settings()
        path = Path(s.data_dir) / "missions" / mission_id / "first_frame.png"
        if not path.is_file():
            raise HTTPException(404, "No first frame.")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/missions/{mission_id}/live-frame")
    def live_frame_image(mission_id: str) -> FileResponse:
        from core.setup_checks import LATEST_NAME

        s = settings()
        path = Path(s.data_dir) / "missions" / mission_id / LATEST_NAME
        if not path.is_file():
            raise HTTPException(404, "No live frame.")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/missions/{mission_id}/frame-watch")
    def frame_watch_image(mission_id: str) -> FileResponse:
        from core.frame_watch import FIGURE_NAME

        s = settings()
        path = Path(s.data_dir) / "missions" / mission_id / FIGURE_NAME
        if not path.is_file():
            raise HTTPException(404, "No frame-watch figure.")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/missions/{mission_id}/frames")
    def frames(mission_id: str) -> dict:
        s = settings()
        items = list_mission_frames(Path(s.data_dir) / "missions" / mission_id)
        return {
            "frames": [
                {
                    "index": i,
                    "label": item["label"],
                    "kind": item["kind"],
                    "url": f"/api/v1/missions/{quote(mission_id)}/frames/{i}",
                }
                for i, item in enumerate(items)
            ]
        }

    @app.get("/api/v1/missions/{mission_id}/frames/{index}")
    def frame_image(mission_id: str, index: int) -> Response:
        from matplotlib import image as mpimg

        s = settings()
        items = list_mission_frames(Path(s.data_dir) / "missions" / mission_id)
        if index < 0 or index >= len(items):
            raise HTTPException(404, "No such frame.")
        chosen = items[index]
        if chosen["kind"] == "preview":
            return FileResponse(chosen["path"], media_type="image/png")
        try:
            view = preview_image(chosen["path"])
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"This frame could not be shown: {exc}") from exc
        buf = BytesIO()
        mpimg.imsave(buf, view, cmap="gray", vmin=0, vmax=1, format="png")
        return Response(buf.getvalue(), media_type="image/png")

    @app.get("/api/v1/artifacts/{artifact_id}")
    def download_artifact(artifact_id: int) -> FileResponse:
        s = settings()
        db = _db(s)
        if db is None:
            raise HTTPException(404, "No database.")
        for art in db.list_artifacts():
            if art["id"] == artifact_id:
                path = Path(art["path"]) if art.get("path") else None
                if path is None or not path.is_file():
                    raise HTTPException(404, "File is not on disk.")
                return FileResponse(path, filename=path.name)
        raise HTTPException(404, "No such artifact.")

    @app.get("/api/v1/opportunities")
    def opportunities(request: Request, refresh: bool = False) -> dict:
        s = settings()
        simple, hour12 = flags(request)
        return queries.opportunities_payload(
            app.state.opp_cache, s, force=refresh, simple=simple, hour12=hour12, db=_db(s),
        )

    @app.post("/api/v1/opportunities/refresh")
    def refresh_opportunities(request: Request) -> dict:
        s = settings()
        simple, hour12 = flags(request)
        return queries.opportunities_payload(
            app.state.opp_cache, s, force=True, simple=simple, hour12=hour12, db=_db(s),
        )

    @app.get("/api/v1/opportunities/{plugin}/{op_id}/track")
    def opportunity_track(plugin: str, op_id: str, request: Request, window_index: int = 0) -> dict:
        s = settings()
        _simple, hour12 = flags(request)
        track = queries.opportunity_track(app.state.opp_cache, s, plugin, op_id, window_index, hour12=hour12)
        if track is None:
            raise HTTPException(404, "No altitude track for that opportunity.")
        return track

    @app.post("/api/v1/opportunities/plan")
    def plan_opportunity(body: PlanBody) -> dict:
        from plugins import load_plugin

        s = settings()
        db = _db(s)
        if db is None:
            raise HTTPException(400, "No database yet. Run a mission once, or save a plan after a database exists.")
        from dashboard.opportunities import get_reports

        reports, _errors = get_reports(app.state.opp_cache, s, force=False)
        report = next((r for r in reports if r["plugin"] == body.plugin), None)
        if report is None:
            raise HTTPException(404, f"No opportunity list for {body.plugin}.")
        op = next((o for o in report["opportunities"] if str(o.get("id")) == str(body.opportunity_id)), None)
        if op is None:
            raise HTTPException(404, "No such opportunity.")
        if not op.get("windows"):
            raise HTTPException(400, "That target has no window to plan.")
        idx = max(0, min(body.window_index, len(op["windows"]) - 1))
        window = op["windows"][idx]
        target = load_plugin(body.plugin, s).planned_target(op, window)
        new, created = db.plan_observation(body.plugin, target, network=op["network"], impact=op.get("impact"))
        if created:
            return {"ok": True, "created": True, "plan": new, "notice": f"Planned {op['name']}."}
        return {
            "ok": True, "created": False, "plan": new,
            "notice": f"{op['name']} already has an open plan (#{new['id']}) for that window.",
        }

    @app.post("/api/v1/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: int) -> dict:
        s = settings()
        db = _db(s)
        if db is None:
            raise HTTPException(404, "No database.")
        ok = db.cancel_planned(plan_id)
        return {"ok": ok, "notice": "Plan cancelled." if ok else "That plan is already gone."}

    @app.post("/api/v1/plans/{plan_id}/again")
    def plan_again(plan_id: int) -> dict:
        s = settings()
        db = _db(s)
        if db is None:
            raise HTTPException(404, "No database.")
        row = db.get_planned(plan_id)
        if row is None:
            return {"ok": False, "notice": "That plan is already gone."}
        closed = closed_window_sentence(row)
        if closed:
            return {"ok": False, "notice": closed}
        new, created = db.plan_observation(
            row["plugin_name"], row["target"], network=row.get("network"), impact=row.get("impact"), note=row.get("note"),
        )
        if not created:
            return {"ok": True, "created": False, "plan": new, "notice": f"{row['target_name']} already has an open plan (#{new['id']}) for that window."}
        if new["id"] == row["id"]:
            return {"ok": True, "created": True, "plan": new, "notice": f"Planned {row['target_name']} again."}
        return {"ok": True, "created": True, "plan": new, "notice": f"Planned {row['target_name']} again (plan #{new['id']})."}

    @app.post("/api/v1/missions/{mission_id}/plan-again")
    def mission_plan_again(mission_id: str) -> dict:
        s = settings()
        db = _db(s)
        if db is None:
            raise HTTPException(404, "No database.")
        row = db.planned_for_mission(mission_id)
        if row is None:
            raise HTTPException(404, "This mission was not started from a plan.")
        return plan_again(int(row["id"]))

    @app.post("/api/v1/plans/{plan_id}/rehearse")
    def rehearse(plan_id: int) -> dict:
        s = settings()
        db = _db(s)
        if db is None or db.get_planned(plan_id) is None:
            raise HTTPException(404, "No such plan.")
        return queries.do_launch(s, plan_id, rehearse=True)

    @app.post("/api/v1/plans/{plan_id}/start")
    def start_plan(plan_id: int, body: ConfirmBody | None = None) -> dict:
        body = body or ConfirmBody()
        s = settings()
        db = _db(s)
        if db is None:
            raise HTTPException(404, "No database.")
        plan = db.get_planned(plan_id)
        if plan is None:
            raise HTTPException(404, "No such plan.")
        spec = queries.launch_spec(
            plan_id, rehearse=False, data_dir=s.data_dir, default_data_dir=s.data_dir,
        )
        if not body.confirm:
            return {
                "ok": False,
                "needs_confirm": True,
                "warning": queries._START_WARNING.format(name=plan["target_name"]),
                "command": spec.command,
            }
        return queries.do_launch(s, plan_id, rehearse=False)

    @app.post("/api/v1/opportunities/forecast")
    def load_forecast() -> dict:
        from tools.weather import WeatherClient

        s = settings()
        try:
            hours = WeatherClient(s).get_forecast(s.site_latitude_deg, s.site_longitude_deg, days=7)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Could not load the forecast: {exc}") from exc
        return queries.jsonable({
            "hours": hours,
            "source": hours[0].get("source") if hours else None,
            "n": len(hours),
        })

    dist = ROOT / "frontend" / "dist"
    if serve_frontend is None:
        serve_frontend = dist.is_dir()
    if serve_frontend and dist.is_dir():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> FileResponse:
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/")
        def root() -> dict[str, str]:
            return {
                "service": "astronomo",
                "api": "/api/v1",
                "docs": "/docs",
                "note": "Headless API. The bundled UI is `python -m dashboard.api` serving frontend/dist.",
            }

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Astronomo HTTP API and bundled UI (http://localhost:8765).")
    parser.add_argument("--host", default=os.environ.get("ASTRONOMO_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=None, help=f"Default {DEFAULT_PORT}, or the next free port if that is taken.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the UI in a browser.")
    args = parser.parse_args()
    env_port = os.environ.get("ASTRONOMO_API_PORT", "").strip()
    explicit = args.port is not None or bool(env_port)
    wanted = args.port if args.port is not None else int(env_port or DEFAULT_PORT)
    port = choose_port(wanted, explicit=explicit)
    if port != wanted:
        print(f"\n  Port {wanted} is in use by another program, so the dashboard is using {port}.", flush=True)
    url = ui_url(args.host, port)
    print(f"\n  Local URL: {url}\n", flush=True)
    if _want_browser(no_browser=args.no_browser):
        _open_browser(url)
    uvicorn.run("dashboard.api.app:create_app", factory=True, host=args.host, port=port, reload=False)


if __name__ == "__main__":
    main()
