"""Hardware smoke test: the first thing to run when the Seestar arrives.

Connects, reads status and firmware, checks the image share, and (by default) parks.
It never slews and never exposes.  The only things it writes to the scope are the
connection set-up ``SeestarController.connect`` does (claim master client, push clock and
site, raw sub-frame setting) and the final park.  Each step reports PASS / WARN / FAIL /
INFO, raw replies are kept, and the report is saved so a failure can be passed on as-is.

The protocol is community-reverse-engineered, so the point of this test is to find out
*which of the unconfirmed assumptions break* on a real scope, cheaply and safely.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Settings
from tools.astrometry import angular_separation_arcsec
from tools.telescope import VERIFY_FIRMWARE_RANGE, SeestarController, TelescopeError, make_controller

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
_RAW_LIMIT = 700
# Firmware at which community reports say a client-authentication handshake starts to be required.
AUTH_FIRMWARE_HINT = "7.18"


def _raw(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= _RAW_LIMIT else text[:_RAW_LIMIT] + " ...(trimmed)"


def _find_lat_lon(value: Any) -> tuple[float, float] | None:
    """Look for a latitude and longitude in a ``get_user_location`` reply (shape UNCONFIRMED)."""
    if isinstance(value, dict):
        lat = next((value[k] for k in ("lat", "latitude") if k in value), None)
        lon = next((value[k] for k in ("lon", "lng", "longitude") if k in value), None)
        try:
            if lat is not None and lon is not None:
                return float(lat), float(lon)
        except (TypeError, ValueError):
            pass
        for v in value.values():
            found = _find_lat_lon(v)
            if found:
                return found
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
    return None


def check_share(share_path: str | None) -> tuple[str, str]:
    if not share_path:
        return FAIL, "ASTRONOMO_SEESTAR_SHARE_PATH is not set; captured frames cannot be collected without it."
    path = Path(share_path)
    if not path.is_dir():
        return FAIL, f"{path} is not a directory.  Is the Seestar's image share mounted there?"
    try:
        files = [p for p in path.rglob("*") if p.suffix.lower() in {".fit", ".fits"}]
    except OSError as exc:
        return FAIL, f"cannot read {path}: {exc}"
    if not files:
        return WARN, f"{path} is readable but holds no FITS files yet (expected before the first capture)."
    newest = max(files, key=lambda p: p.stat().st_mtime)
    stamp = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return PASS, f"{path} readable; {len(files)} FITS file(s), newest {newest.name} ({stamp})."


def run_hardware_check(settings: Settings, controller: SeestarController | None = None, *, park: bool = True) -> dict[str, Any]:
    """Run every step; never raises.  Returns ``{"ok": bool, "steps": [...], ...}``."""
    steps: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str, raw: Any = None) -> None:
        step = {"name": name, "status": status, "detail": detail}
        if raw is not None:
            step["raw"] = _raw(raw)
        steps.append(step)

    mock = bool(settings.mock_mode) if controller is None else controller.mock
    site_known = settings.site_source != "default"
    if not mock and not site_known and settings.seestar_set_site:
        # Never push the placeholder site onto the scope.
        settings = replace(settings, seestar_set_site=False)
        add("site", WARN, "No observing site is set, so the scope's own location was left alone.  Set it first "
            "(dashboard: Opportunities > Observing site) and run this again.")
    ctl = controller or make_controller(settings)

    connected = False
    try:
        ctl.connect()
        connected = True
        where = (
            "MOCK (no hardware touched)" if mock
            else f"{settings.seestar_host}:{settings.seestar_control_port} ({ctl.transport})"
        )
        add("connect", PASS, f"connected to {where}")
    except TelescopeError as exc:
        hints = (
            "Check the scope is powered and on the same network, that seestar.local resolves (or set "
            "ASTRONOMO_SEESTAR_HOST to its IP), and that the phone app is not holding the connection."
        )
        if ctl.transport == "jsonrpc":
            hints += (
                "  If the firmware is 7.18 or later, the control port wants a handshake this driver does not "
                "perform: try ASTRONOMO_SEESTAR_TRANSPORT=alpaca."
            )
        add("connect", FAIL, f"{exc}.  {hints}")

    if connected and not mock:
        (_read_steps_alpaca if ctl.transport == "alpaca" else _read_steps)(ctl, settings, site_known, add)
        if park:
            try:
                ctl.park()
                add("park", PASS, "park command accepted")
            except TelescopeError as exc:
                add("park", FAIL, str(exc))
        else:
            add("park", INFO, "skipped (--no-park)")
    elif connected:
        add("status", INFO, "mock mode: the status, firmware, share and park steps need a real scope")

    if connected:
        ctl.disconnect()

    report = {
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "mock" if mock else "real",
        "host": settings.seestar_host,
        "firmware": ctl.firmware,
        "ok": not any(s["status"] == FAIL for s in steps),
        "steps": steps,
    }
    return report


def _read_steps(ctl: SeestarController, settings: Settings, site_known: bool, add) -> None:
    fw = ctl.firmware
    lo, hi = VERIFY_FIRMWARE_RANGE
    if fw is None:
        add("firmware", WARN, "the firmware version could not be read from get_device_state; the verify-injection "
            "rule was not applied by version.", ctl.device_state)
    else:
        note = f"verify injection {'ON' if lo < fw < hi else 'off'} for this version"
        add("firmware", PASS, f"firmware {fw}; {note}", ctl.device_state)

    for name, key in (
        ("connection test", "test_connection"),
        ("position", "get_coord"),
        ("camera", "camera_info"),
        ("disk", "disk_volume"),
    ):
        try:
            result = ctl.query(key)
            add(name, INFO, "reply recorded (compare it with what the app shows)", result)
        except TelescopeError as exc:
            add(name, WARN, f"{key} was refused or unsupported on this firmware: {exc}")

    try:
        loc = ctl.query("get_location")
        add("scope's location", INFO, "reply recorded", loc)
        pair = _find_lat_lon(loc)
        if pair and site_known:
            off_km = angular_separation_arcsec(settings.site_longitude_deg, settings.site_latitude_deg, pair[1], pair[0]) / 3600.0 * 111.0
            status = PASS if off_km < 5.0 else WARN
            add("location matches site", status,
                f"the scope has ({pair[0]:.3f}, {pair[1]:.3f}); the site profile has "
                f"({settings.site_latitude_deg:.3f}, {settings.site_longitude_deg:.3f}); about {off_km:.0f} km apart.  "
                "If this is large, check the longitude sign convention before trusting any goto.")
    except TelescopeError as exc:
        add("scope's location", WARN, f"get_user_location was refused or unsupported: {exc}")

    add("share", *check_share(settings.seestar_share_path))


def _read_steps_alpaca(ctl: Any, settings: Settings, site_known: bool, add) -> None:
    """The read-only steps for the Alpaca transport (it has none of the JSON-RPC status calls)."""
    state = ctl.device_state or {}
    add("firmware", INFO, "Alpaca does not report the firmware build.  Read it in the Seestar app and write the version "
        "down (7.18 or later is what needed the handshake on the JSON-RPC port).", state)

    eq = state.get("equatorial_system")
    if eq == 2:
        add("coordinate system", PASS, "the mount reports J2000 coordinates, which is what Astronomo sends.")
    else:
        add("coordinate system", WARN, f"the mount reports equatorialsystem={eq!r} (2 means J2000).  If it is not J2000 "
            "a goto can be off by up to about 0.4 degrees; check the pointing at first light.")

    for name, props in (("position", ("rightascension", "declination", "altitude", "azimuth", "tracking", "atpark")),
                        ("camera", ("cameraxsize", "cameraysize", "gain", "ccdtemperature", "camerastate"))):
        values: dict[str, Any] = {}
        for prop in props:
            try:
                values[prop] = ctl.read_property("telescope" if name == "position" else "camera", prop)
            except TelescopeError as exc:
                values[prop] = f"unavailable: {exc}"
        add(name, INFO, "reply recorded (compare it with what the app shows)", values)

    try:
        pair = (float(ctl.read_property("telescope", "sitelatitude")), float(ctl.read_property("telescope", "sitelongitude")))
        add("scope's location", INFO, "reply recorded", {"lat": pair[0], "lon": pair[1]})
        if site_known:
            off_km = angular_separation_arcsec(settings.site_longitude_deg, settings.site_latitude_deg, pair[1], pair[0]) / 3600.0 * 111.0
            add("location matches site", PASS if off_km < 5.0 else WARN,
                f"the scope has ({pair[0]:.3f}, {pair[1]:.3f}); the site profile has "
                f"({settings.site_latitude_deg:.3f}, {settings.site_longitude_deg:.3f}); about {off_km:.0f} km apart.  "
                "ASCOM longitude is positive east; if this is large, check the sign before trusting any goto.")
    except (TelescopeError, TypeError, ValueError) as exc:
        add("scope's location", WARN, f"site latitude and longitude could not be read over Alpaca: {exc}")

    _focuser_step(ctl, add)
    add("share", INFO, "not used over Alpaca: frames come back from the camera, not from the image share.")


def _focuser_step(ctl: Any, add) -> None:
    """Read the focuser's position and range.  Reads only: nothing is moved (``main.py --focus`` moves it)."""
    try:
        number = ctl.focuser_number()
    except (TelescopeError, AttributeError) as exc:
        add("focuser", WARN, f"no focuser found: {exc}.  Autofocus (main.py --focus) will not work until one is listed.")
        return
    values: dict[str, Any] = {"device_number": number}
    try:
        # Alpaca devices answer property reads only once connected (an unconnected read gave 1031 and maxstep 0 on 2026-11-16).
        ctl.focuser(None).connect()
    except Exception as exc:  # noqa: BLE001 - the reads below say what is missing
        values["connect"] = f"failed: {exc}"
    for prop in ("position", "maxstep", "maxincrement", "absolute", "stepsize", "ismoving", "temperature", "tempcomp"):
        try:
            values[prop] = ctl.read_property("focuser", prop, number)
        except TelescopeError as exc:
            values[prop] = f"unavailable: {exc}"
    usable = isinstance(values.get("position"), (int, float)) and isinstance(values.get("maxstep"), (int, float))
    if usable:
        add("focuser", INFO, f"device {number} at position {values['position']} of 0 to {values['maxstep']}; compare it "
            "with the app's focus screen.  Whether Move works while tracking is not confirmed.", values)
    else:
        add("focuser", WARN, f"device {number} did not report a position and range.  It may need to be connected first; "
            "main.py --focus connects it.", values)


def format_report(report: dict[str, Any]) -> str:
    lines = [f"Seestar hardware check ({report['mode']}, {report['when']})"]
    for step in report["steps"]:
        lines.append(f"  [{step['status']:<4}] {step['name']}: {step['detail']}")
        if step.get("raw"):
            lines.append(f"           raw: {step['raw']}")
    lines.append("RESULT: " + ("OK" if report["ok"] else "FAILED -- fix the FAIL lines above before a first-light run"))
    return "\n".join(lines)


def save_report(report: dict[str, Any], data_dir: Path) -> Path:
    out = Path(data_dir) / "hardware_checks"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{report['when'].replace(':', '').replace('-', '')}.json"
    path.write_text(json.dumps(report, indent=2))
    return path
