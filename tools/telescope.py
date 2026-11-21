"""Seestar S50 controller: real hardware over TCP, or a faithful mock.

Mock mode (``settings.mock_mode``)
    No socket is ever opened.  Each frame "exposes" for ``settings.mock_capture_seconds``
    of wall-clock time (10 s by default) and writes a synthetic FITS file produced by
    :mod:`tools.mock_sky`: a fixed star field, hot pixels, noise and a pointing error, plus
    whatever the plugin's :class:`~tools.mock_sky.SkyScene` adds (``capture_sequence(scene=...)``).
    The telescope itself knows nothing about the science case.

Real mode
    Speaks the Seestar's JSON-RPC-over-TCP control protocol (port 4700, newline
    delimited).  The method names live in ``_METHODS`` so they are easy to adapt to
    firmware differences.

    IMPORTANT: the real-hardware path has been written against the *community*
    reverse-engineered protocol (seestar_alp, seestarpy, scopinator, the ARGOS S30 Pro
    protocol notes).  ZWO publishes no spec for it, and nothing here has been exercised
    against a physical Seestar.  Every behaviour marked ``UNCONFIRMED`` below is a
    first-light check, not a fact.  The Seestar does not stream FITS over the control
    socket; frames are collected from the device's image share, which you must mount
    locally and point ``ASTRONOMO_SEESTAR_SHARE_PATH`` at.  Treat first runs as supervised.

    What the driver does beyond the bare commands (all UNCONFIRMED until a real run):

    * **Firmware-dependent verify injection.**  Community notes say firmware 2583-2704
      wants ``"verify": true`` on every request.  We read the firmware version on connect
      and inject only in that window (and if a plain request is refused, we try once with it).
    * **Master claim.**  ``set_setting {"master_cli": true}`` so a second client (the phone
      app) does not silently take over; a refusal is logged, not fatal.
    * **Site and clock.**  ``set_user_location`` / ``pi_set_time`` from the site profile
      (``ASTRONOMO_SEESTAR_SET_SITE=0`` to skip).
    * **Arm.**  ``scope_move_to_horizon`` once per connection before the first slew.
    * **Heartbeat.**  A cheap status call every few seconds during long waits so an idle
      connection is not dropped.
    * **Reconnect and retry.**  A dropped connection is re-opened, the connection set-up is
      replayed, and the call is retried with exponential backoff.  Device errors (non-zero
      reply code) are never retried.
    * **Raw sub-frames.**  ``set_stack_setting {"save_discrete_frame": true}`` and a
      preference for sub-frame files over the live stack when collecting.  Whether
      single-frame photometry is possible on the real scope is a first-light question.

    NOT implemented, on purpose: the client-authentication handshake some newer firmware
    (reported as 7.18 and later) demands.  See ``docs/notes/firmware-and-authentication.md`` for why, and for the
    options.  If the scope demands it, :meth:`SeestarController.connect` fails with a
    message that says so rather than looping.  The route around it is the Alpaca transport
    (``ASTRONOMO_SEESTAR_TRANSPORT=alpaca``, :mod:`tools.seestar_alpaca`), which needs no handshake.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from astropy.io import fits

from config import Settings
from tools.mock_sky import MockSky, SkyScene

log = logging.getLogger(__name__)

HEARTBEAT_S = 8.0  # status ping interval during long waits (UNCONFIRMED that it is needed)
# Firmware window in which community notes say every request needs "verify": true.
VERIFY_FIRMWARE_RANGE = (2582, 2705)  # exclusive on both ends
_FIRMWARE_KEYS = ("firmware_ver_int", "fw_ver_int", "ver_int")


class TelescopeError(RuntimeError):
    """Hardware or protocol failure.

    ``transient`` marks transport problems (dropped connection, timeout) that a reconnect
    may cure; device-reported errors are not transient and are never retried.
    """

    def __init__(self, message: str, *, transient: bool = False, code: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.code = code


class StopRequested(TelescopeError):
    """The operator asked this mission to park and stay stopped. Not a fault, and not retried."""


_METHODS = {
    "start_view": "iscope_start_view",
    "stop_view": "iscope_stop_view",
    "start_stack": "iscope_start_stack",
    "set_setting": "set_setting",
    "set_control": "set_control_value",
    "get_coord": "scope_get_equ_coord",
    "park": "scope_park",
    "raise_arm": "scope_move_to_horizon",
    "set_location": "set_user_location",
    "set_time": "pi_set_time",
    "set_stack_setting": "set_stack_setting",
    "device_state": "get_device_state",
    "test_connection": "test_connection",
    "camera_info": "get_camera_info",
    "disk_volume": "get_disk_volume",
    "get_location": "get_user_location",
    "start_solve": "start_solve",
    "solve_result": "get_solve_result",
}


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "target"


def inject_verify(params: Any) -> Any:
    """Add the ``verify`` flag the way the community clients do (shape depends on params)."""
    if params is None:
        return ["verify"]
    if isinstance(params, dict):
        return {**params, "verify": True}
    if isinstance(params, list):
        return [*params, "verify"]
    return [params, "verify"]


def find_firmware_int(result: Any) -> int | None:
    """Dig the integer firmware version out of a ``get_device_state`` reply (key names vary)."""
    if isinstance(result, dict):
        for key in _FIRMWARE_KEYS:
            if key in result:
                try:
                    return int(result[key])
                except (TypeError, ValueError):
                    pass
        for value in result.values():
            found = find_firmware_int(value)
            if found is not None:
                return found
    return None


class _SeestarRpc:
    """Minimal newline-delimited JSON-RPC client (thread-safe, single connection).

    Transport failures are retried with backoff: the socket is closed and re-opened,
    ``on_reconnect`` replays the connection set-up, and the call is repeated.
    """

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        *,
        retries: int = 0,
        backoff_s: float = 2.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.host, self.port, self.timeout = host, port, timeout
        self.retries, self.backoff_s, self._sleep = retries, backoff_s, sleep_fn
        self.inject_verify = False
        self.on_reconnect: Callable[[], None] | None = None
        self.reconnects = 0
        self._sock: socket.socket | None = None
        self._file = None
        self._next_id = 1000
        self._lock = threading.Lock()

    def open(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise TelescopeError(
                f"cannot connect to Seestar at {self.host}:{self.port}: {exc}", transient=True
            ) from exc
        self._sock.settimeout(self.timeout)
        self._file = self._sock.makefile("rwb", buffering=0)

    def close(self) -> None:
        for obj in (self._file, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._file = self._sock = None

    def open_with_retry(self) -> None:
        """Open the connection, backing off between attempts (the scope may still be booting)."""
        attempt = 0
        while True:
            try:
                self.open()
                return
            except TelescopeError as exc:
                if attempt >= self.retries:
                    raise
                attempt += 1
                self._backoff(attempt, "connect", exc)

    def _backoff(self, attempt: int, what: str, exc: Exception) -> None:
        delay = self.backoff_s * 2 ** (attempt - 1)
        log.warning("Telescope: %s failed (%s); retry %d/%d in %.1fs", what, exc, attempt, self.retries, delay)
        self._sleep(delay)

    def call(self, method: str, params: Any = None, *, retry: bool = True) -> Any:
        attempt = 0
        while True:
            try:
                return self._call_once(method, params)
            except TelescopeError as exc:
                if not (retry and exc.transient) or attempt >= self.retries:
                    raise
                attempt += 1
                self._backoff(attempt, method, exc)
                self._reconnect()

    def _reconnect(self) -> None:
        self.close()
        self.open_with_retry()
        self.reconnects += 1
        if self.on_reconnect is not None:
            self.on_reconnect()  # replays set-up with retry=False, so no recursion

    def _call_once(self, method: str, params: Any) -> Any:
        if self._file is None:
            raise TelescopeError("not connected", transient=True)
        if self.inject_verify:
            params = inject_verify(params)
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            payload: dict[str, Any] = {"id": req_id, "method": method}
            if params is not None:
                payload["params"] = params
            try:
                self._file.write((json.dumps(payload) + "\r\n").encode())
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    line = self._file.readline()
                    if not line:
                        raise TelescopeError("connection closed by Seestar", transient=True)
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("id") != req_id:
                        continue  # async "Event" notifications and stale replies
                    code = msg.get("code", 0)
                    if code != 0:
                        raise TelescopeError(f"{method} failed: {msg.get('error', msg)}", code=code)
                    return msg.get("result")
            except (OSError, socket.timeout) as exc:
                raise TelescopeError(f"{method}: socket error: {exc}", transient=True) from exc
            raise TelescopeError(f"{method}: timed out waiting for reply", transient=True)


def make_controller(
    settings: Settings, mock: bool | None = None, sleep_fn: Callable[[float], None] = time.sleep
) -> "SeestarController":
    """The controller for ``settings.seestar_transport`` (mock mode is the same whichever is chosen)."""
    is_mock = settings.mock_mode if mock is None else mock
    if not is_mock and settings.seestar_transport == "alpaca":
        from tools.seestar_alpaca import AlpacaController  # imported late: it subclasses SeestarController

        return AlpacaController(settings, mock=is_mock, sleep_fn=sleep_fn)
    return SeestarController(settings, mock=is_mock, sleep_fn=sleep_fn)


class SeestarController:
    """High-level telescope API used by the graph's ``execute_observation`` node.

    This class speaks the JSON-RPC control protocol; ``tools.seestar_alpaca.AlpacaController``
    is the same API over ASCOM Alpaca.  Use :func:`make_controller` to get the configured one.
    """

    transport = "jsonrpc"

    def __init__(
        self,
        settings: Settings,
        mock: bool | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.mock = settings.mock_mode if mock is None else mock
        self._sleep = sleep_fn
        self._rpc: _SeestarRpc | None = None
        self.connected = False
        self.pointing: dict[str, Any] | None = None
        self.firmware: int | None = None
        self.device_state: Any = None
        self._arm_raised = False
        self.park_calls = 0
        self.stop_check: Callable[[], bool] | None = None
        #: Battery percent, when a test or a transport has one. None: the telescope did not report it.
        self.battery_pct: float | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        if self.connected:
            return
        if self.mock:
            log.info("Telescope: MOCK mode -- no hardware connection opened")
        else:
            s = self.settings
            self._rpc = _SeestarRpc(
                s.seestar_host, s.seestar_port, s.seestar_timeout_s,
                retries=s.seestar_retries, backoff_s=s.seestar_backoff_s, sleep_fn=self._sleep,
            )
            self._rpc.open_with_retry()
            log.info("Telescope: connected to Seestar at %s:%s", s.seestar_host, s.seestar_port)
            try:
                self._initialise()
            except TelescopeError:
                self._rpc.close()
                self._rpc = None
                raise
            self._rpc.on_reconnect = self._initialise
        self.connected = True

    def _initialise(self) -> None:
        """Per-connection set-up.  Runs on every (re)connect, never retries itself."""
        rpc = self._require_rpc()
        self._arm_raised = False
        self.device_state = self._read_device_state(rpc)
        self.firmware = find_firmware_int(self.device_state)
        if self.firmware is not None:
            lo, hi = VERIFY_FIRMWARE_RANGE
            rpc.inject_verify = lo < self.firmware < hi
            log.info("Telescope: firmware %s (verify injection %s)", self.firmware, "on" if rpc.inject_verify else "off")
        else:
            log.warning("Telescope: could not read the firmware version from get_device_state")

        # Best-effort extras: a refusal is logged, never fatal.
        self._try(rpc, _METHODS["set_setting"], {"master_cli": True}, "claim master client")
        if self.settings.seestar_set_site:
            now = datetime.now(timezone.utc)
            self._try(
                rpc, _METHODS["set_time"],
                [{"year": now.year, "mon": now.month, "day": now.day, "hour": now.hour,
                  "min": now.minute, "sec": now.second, "time_zone": "UTC"}],
                "set clock",
            )
            self._try(
                rpc, _METHODS["set_location"],
                {"lat": self.settings.site_latitude_deg, "lon": self.settings.site_longitude_deg, "force": True},
                "set site location",
            )
        self._try(rpc, _METHODS["set_stack_setting"], {"save_discrete_frame": True}, "enable raw sub-frames")

    def _read_device_state(self, rpc: _SeestarRpc) -> Any:
        """Read the device state, trying a plain request first and a verified one second."""
        try:
            return rpc.call(_METHODS["device_state"], retry=False)
        except TelescopeError as first:
            if first.transient or rpc.inject_verify:
                raise
            rpc.inject_verify = True
            try:
                state = rpc.call(_METHODS["device_state"], retry=False)
                log.info("Telescope: the scope only answers requests carrying 'verify'; injecting it")
                return state
            except TelescopeError:
                rpc.inject_verify = False
                raise TelescopeError(
                    f"the Seestar refused get_device_state ({first}).  If its firmware is 7.18 or later it may "
                    "require a client-authentication handshake that this driver does not perform.  Try "
                    "ASTRONOMO_SEESTAR_TRANSPORT=alpaca (no handshake); see docs/notes/firmware-and-authentication.md for the options.",
                    code=first.code,
                ) from first

    @staticmethod
    def _try(rpc: _SeestarRpc, method: str, params: Any, what: str) -> None:
        try:
            rpc.call(method, params, retry=False)
        except TelescopeError as exc:
            if exc.transient:
                raise
            log.warning("Telescope: could not %s (%s) -- continuing", what, exc)

    def disconnect(self) -> None:
        if self._rpc is not None:
            self._rpc.close()
            self._rpc = None
        self.connected = False

    def __enter__(self) -> "SeestarController":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if exc[0] is not None:  # leave the mount safe on failure
                self.park()
        except TelescopeError:
            log.exception("Telescope: failed to park after error")
        finally:
            self.disconnect()

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    def slew_to(self, target: dict, filter_name: str = "") -> None:
        ra, dec = float(target["ra_deg"]), float(target["dec_deg"])
        log.info("Telescope: slewing to %s (RA %.4f deg, Dec %.4f deg)", target["name"], ra, dec)
        self.pointing = {"name": target["name"], "ra_deg": ra, "dec_deg": dec}
        if self.mock:
            self._sleep(min(1.0, self.settings.mock_capture_seconds / 10.0))
            return
        rpc = self._require_rpc()
        if not self._arm_raised:
            self._try(rpc, _METHODS["raise_arm"], None, "raise the arm")
            self._arm_raised = True
        rpc.call(
            _METHODS["start_view"],
            {
                "mode": "star",
                "target_ra_dec": [ra / 15.0, dec],  # RA in hours (UNCONFIRMED: check against the app at first light)
                "target_name": target["name"],
                "lp_filter": filter_name.upper() in {"LP", "LPR", "LP_FILTER"},
            },
        )
        self._wait_until_on_target(ra, dec)

    def configure(self, plan: dict) -> None:
        log.info(
            "Telescope: configure gain=%s exposure=%.1fs filter=%s",
            plan["gain"], plan["exposure_s"], plan["filter"],
        )
        if self.mock:
            return
        rpc = self._require_rpc()
        rpc.call(_METHODS["set_control"], ["gain", int(plan["gain"])])
        rpc.call(_METHODS["set_setting"], {"exp_ms": {"stack_l": int(plan["exposure_s"] * 1000)}})

    def park(self) -> None:
        self.park_calls += 1
        if self.mock or self._rpc is None:
            return
        self._rpc.call(_METHODS["park"])

    def battery_percent(self) -> float | None:
        """The telescope's battery, if a transport has put one on ``battery_pct``. None means no reading."""
        return self.battery_pct

    def _raise_if_stopped(self) -> None:
        if self.stop_check is not None and self.stop_check():
            raise StopRequested("stopped by the operator")

    def query(self, name: str, params: Any = None) -> Any:
        """Read-only helper for diagnostics: call one of the named ``_METHODS`` and return the raw result."""
        if self.mock:
            return None
        return self._require_rpc().call(_METHODS[name], params)

    def focuser(self, target: dict | None = None):
        """The focuser (and the camera behind it) for ``tools.autofocus``.

        Simulation gets a focuser with a hidden best position.  The JSON-RPC transport has none: the
        scope's own autofocus lives behind the handshake this driver does not perform, so a real
        focus run needs ``ASTRONOMO_SEESTAR_TRANSPORT=alpaca``.
        """
        if self.mock:
            from tools.autofocus import MockFocuser

            return MockFocuser(target or {"name": "Focus field", "ra_deg": 150.0, "dec_deg": 11.0, "sim": {"seed": 3}})
        raise TelescopeError("focusing from here needs the Alpaca transport (ASTRONOMO_SEESTAR_TRANSPORT=alpaca)")

    def capture_sequence(
        self,
        target: dict,
        plan: dict,
        out_dir: str | Path,
        scene: SkyScene | None = None,
        after_frame: Callable[[int, str], None] | None = None,
        before_exposures: Callable[[], None] | None = None,
    ) -> list[str]:
        """Slew, configure and capture ``plan['count']`` frames; return FITS paths.

        ``scene`` is the plugin's simulated-sky content; it is used in mock mode only.
        ``after_frame(index, path)`` runs once each frame is on disk, before the pause
        that precedes the next one. An exception from it stops the sequence.
        ``before_exposures()`` runs once the scope is on target and configured, before the
        first exposure (the mission focuses there).
        """
        if not self.connected:
            raise TelescopeError("capture_sequence called before connect()")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)

        self.slew_to(target, str(plan.get("filter", "")))
        self.configure(plan)
        if before_exposures is not None:
            before_exposures()
        if self.mock:
            return self._capture_mock(target, plan, out, scene, after_frame)
        return self._capture_real(target, plan, out, after_frame)

    # ------------------------------------------------------------------ #
    # Pointing verification (the scope's own plate solver)
    # ------------------------------------------------------------------ #
    def solve_pointing(self, timeout_s: float = 60.0) -> dict[str, Any] | None:
        """Ask the scope to plate-solve what it is looking at; return ``{"ra_deg","dec_deg"}``.

        UNCONFIRMED: method names (``start_solve`` / ``get_solve_result``) and the reply shape
        come from community client sources.  Returns None when the reply cannot be understood,
        so callers must treat that as "not verified", never as "verified".
        """
        if self.mock:
            return None
        rpc = self._require_rpc()
        rpc.call(_METHODS["start_solve"])
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                result = rpc.call(_METHODS["solve_result"])
            except TelescopeError as exc:
                if exc.transient:
                    raise
                result = None
            parsed = parse_solve_result(result)
            if parsed is not None:
                return parsed
            if time.monotonic() >= deadline:
                return None
            self._idle(2.0)

    # ------------------------------------------------------------------ #
    # Mock capture
    # ------------------------------------------------------------------ #
    def _capture_mock(
        self, target: dict, plan: dict, out: Path, scene: SkyScene | None = None,
        after_frame: Callable[[int, str], None] | None = None,
    ) -> list[str]:
        sky = MockSky.from_target(target, now=datetime.now(timezone.utc), scene=scene)
        exposure = float(plan["exposure_s"])
        interval = float(plan.get("interval_s", 0.0))
        paths: list[str] = []
        for k in range(int(plan["count"])):
            self._raise_if_stopped()
            log.info("Telescope[MOCK]: exposing frame %d/%d (%.0f s)", k + 1, plan["count"], self.settings.mock_capture_seconds)
            self._sleep(self.settings.mock_capture_seconds)
            # Simulated clock: frames are spaced by exposure + interval regardless of
            # how fast the wall clock ran, so motion is reproducible with --fast.
            start = sky.epoch0 + timedelta(seconds=k * (exposure + interval))
            data = sky.render(start, exposure, k)
            header = sky.header(start, exposure, int(plan["gain"]), str(plan["filter"]), k, target["name"])
            path = out / f"frame_{k + 1:03d}.fits"
            fits.PrimaryHDU(data=data, header=header).writeto(path, overwrite=True)
            paths.append(str(path))
            if after_frame is not None:
                after_frame(k, paths[-1])
        log.info("Telescope[MOCK]: captured %d frame(s) for %s", len(paths), target["name"])
        return paths

    # ------------------------------------------------------------------ #
    # Real capture
    # ------------------------------------------------------------------ #
    def _require_rpc(self) -> _SeestarRpc:
        if self._rpc is None:
            raise TelescopeError("not connected to a Seestar")
        return self._rpc

    def _idle(self, seconds: float) -> None:
        """Wait ``seconds`` without letting the connection go quiet (heartbeat every few seconds)."""
        remaining = float(seconds)
        while remaining > 0:
            step = min(HEARTBEAT_S, remaining)
            self._sleep(step)
            remaining -= step
            if remaining > 0 and self._rpc is not None:
                self._rpc.call(_METHODS["get_coord"])

    def _wait_until_on_target(self, ra_deg: float, dec_deg: float, timeout_s: float = 180.0) -> None:
        rpc = self._require_rpc()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            coord = rpc.call(_METHODS["get_coord"]) or {}
            try:
                cur_ra, cur_dec = float(coord["ra"]) * 15.0, float(coord["dec"])
            except (KeyError, TypeError, ValueError):
                cur_ra = cur_dec = float("nan")
            dra = ((cur_ra - ra_deg + 180.0) % 360.0) - 180.0
            if abs(dra) < 0.2 and abs(cur_dec - dec_deg) < 0.2:
                return
            self._sleep(2.0)
        raise TelescopeError("timed out waiting for the Seestar to reach the target")

    def _capture_real(
        self, target: dict, plan: dict, out: Path, after_frame: Callable[[int, str], None] | None = None
    ) -> list[str]:
        rpc = self._require_rpc()
        share = self.settings.seestar_share_path
        if not share or not Path(share).is_dir():
            raise TelescopeError(
                "ASTRONOMO_SEESTAR_SHARE_PATH must point at the mounted Seestar image share "
                "so captured FITS frames can be collected"
            )
        exposure = float(plan["exposure_s"])
        interval = float(plan.get("interval_s", 0.0))
        paths: list[str] = []
        for k in range(int(plan["count"])):
            self._raise_if_stopped()
            started = time.time()
            log.info("Telescope: exposing frame %d/%d (%.0f s)", k + 1, plan["count"], exposure)
            rpc.call(_METHODS["start_stack"], {"restart": True})
            self._idle(exposure + 3.0)
            rpc.call(_METHODS["stop_view"], {"stage": "Stack"})
            paths.append(self._collect_fits(Path(share), started, out / f"frame_{k + 1:03d}.fits", target, plan))
            if after_frame is not None:
                after_frame(k, paths[-1])
            if interval > 0 and k < int(plan["count"]) - 1:
                self._idle(interval)
        return paths

    def _collect_fits(self, share: Path, since: float, dest: Path, target: dict, plan: dict) -> str:
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            candidates = [
                p for p in share.rglob("*")
                if p.suffix.lower() in {".fit", ".fits"} and p.stat().st_mtime >= since - 1.0
            ]
            if candidates:
                # Prefer raw sub-frames (UNCONFIRMED: the community layout puts them in a
                # "<target>_sub" folder) over the live-stacked image; fall back to anything new.
                subs = [p for p in candidates if "_sub" in str(p.parent.name).lower()]
                newest = max(subs or candidates, key=lambda p: p.stat().st_mtime)
                time.sleep(1.0)  # let the device finish writing
                shutil.copyfile(newest, dest)
                self._ensure_pointing_headers(dest, target, plan)
                return str(dest)
            self._idle(2.0)
        raise TelescopeError("no new FITS file appeared on the Seestar share after exposure")

    @staticmethod
    def _ensure_pointing_headers(path: Path, target: dict, plan: dict) -> None:
        """Make sure downstream code can rely on OBJECT/RA/DEC/EXPTIME being present."""
        with fits.open(path, mode="update") as hdul:
            hdr = hdul[0].header
            hdr.setdefault("OBJECT", target["name"])
            hdr.setdefault("RA", float(target["ra_deg"]))
            hdr.setdefault("DEC", float(target["dec_deg"]))
            hdr.setdefault("EXPTIME", float(plan["exposure_s"]))
            if "DATE-OBS" not in hdr:
                hdr["DATE-OBS"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def parse_solve_result(result: Any) -> dict[str, Any] | None:
    """Read a plate-solve reply into degrees, or None if it does not look like a solution.

    Accepts the shapes the community clients show: ``{"ra_dec": [ra_hours, dec]}`` or
    ``{"ra": hours, "dec": deg}``, optionally nested under ``result``.  UNCONFIRMED.
    """
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("result"), dict):
        result = result["result"]
    ra = dec = None
    pair = result.get("ra_dec")
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        ra, dec = pair
    elif "ra" in result and "dec" in result:
        ra, dec = result["ra"], result["dec"]
    try:
        return {"ra_deg": float(ra) * 15.0, "dec_deg": float(dec)}
    except (TypeError, ValueError):
        return None
