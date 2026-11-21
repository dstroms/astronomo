"""Seestar over ASCOM Alpaca: a second transport that needs no client-authentication handshake.

Why this exists
    Firmware 7.18 and later demand an RSA challenge-response handshake on the JSON-RPC port
    (4700) that :mod:`tools.telescope` deliberately does not perform (see
    ``docs/notes/firmware-and-authentication.md``).  The community reports that the
    same firmware also runs a standard ASCOM Alpaca REST server on port 32323 which needs no
    key, and that a Seestar S30 Pro on firmware 7.18 was driven through it (connect, slew, park,
    gain, exposure, image download).  So this transport is the first thing to try on new firmware.

What is and is not known
    Everything here is written from community notes (the ARGOS S30 Pro protocol reference, a
    Cloudy Nights S50 technical reference, ``seestar_alp``) and the public ASCOM Alpaca
    specification.  ZWO publishes nothing, and **nothing here has run against a physical
    Seestar**.  The tests use a fake Alpaca server written from the same notes, which proves the
    code is self-consistent, not that it matches the firmware.  Behaviour marked ``UNCONFIRMED`` is
    a first-light check.

Differences from the JSON-RPC transport
    * Frames come back from the camera as single raw exposures (``startexposure`` then
      ``imagearray``), not from the image share, so ``ASTRONOMO_SEESTAR_SHARE_PATH`` is not used.
    * There is no plate-solve call, so :meth:`AlpacaController.solve_pointing` returns None
      ("not verified") and the first-frame pointing check falls back to Astronomo's own solver.
    * The filter is not selected (the position map is unconfirmed for the S50), and the frame
      header does not claim one.
    * The scope's clock cannot be trusted or set the way the app does it; each frame's DATE-OBS
      comes from the camera's own ``lastexposurestarttime`` if it answers, else from this
      computer's clock, and the header says which (``TIMESRC``).  A transit timing is only as
      good as this, so check it at first light.
"""

from __future__ import annotations

import logging
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import requests
from astropy.io import fits

from config import Settings
from tools.astrometry import nominal_pixel_scale_arcsec
from tools.telescope import SeestarController, TelescopeError

log = logging.getLogger(__name__)

# ASCOM Alpaca "ImageBytes" binary image format (metadata version 1): eleven little-endian int32.
_IMAGEBYTES_MIME = "application/imagebytes"
_IMAGEBYTES_HEADER = struct.Struct("<11i")
# ASCOM ImageArrayElementTypes. 7 is Int64 and 8 is UInt16; a Seestar S50 Pro on firmware 9.75
# sends a rank-2 frame as type 8.
_IMAGEBYTES_DTYPES = {1: "<i2", 2: "<i4", 3: "<f8", 4: "<f4", 5: "<u8", 6: "u1", 7: "<i8", 8: "<u2", 9: "<u4"}
# A full-frame download as JSON took about 33 s on an S30 Pro; ImageBytes about 3 s.
IMAGE_TIMEOUT_S = 180.0
# Extra time allowed after the exposure for readout before giving up on ``imageready``.
READOUT_ALLOWANCE_S = 90.0
SLEW_TIMEOUT_S = 180.0
# ASCOM EquatorialSystem values.
_EQUATORIAL_SYSTEMS = {0: "other", 1: "topocentric (JNow)", 2: "J2000", 3: "J2050", 4: "B1950"}


class AlpacaClient:
    """Minimal ASCOM Alpaca REST client (one host, one session, thread-safe enough for one controller).

    Transport failures (connection refused or reset, timeouts) are retried with exponential
    backoff.  A reply carrying a non-zero ``ErrorNumber`` is a device error and is never retried.
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
        client_id: int = 4271,
    ):
        self.base = f"http://{host}:{port}"
        self.timeout, self.retries, self.backoff_s, self._sleep = timeout, retries, backoff_s, sleep_fn
        self.client_id = client_id
        self._tx = 0
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    # -- plumbing ------------------------------------------------------------ #
    def _params(self) -> dict[str, int]:
        self._tx += 1
        return {"ClientID": self.client_id, "ClientTransactionID": self._tx}

    def _request(self, method: str, url: str, *, timeout: float, retry: bool, **kwargs: Any) -> requests.Response:
        attempt = 0
        while True:
            try:
                resp = self._session.request(method, url, timeout=timeout, **kwargs)
                if resp.status_code >= 400:
                    raise TelescopeError(f"{method} {url}: HTTP {resp.status_code} {resp.text[:200]!r}")
                return resp
            except requests.RequestException as exc:
                err = TelescopeError(f"{method} {url}: {exc}", transient=True)
                if not retry or attempt >= self.retries:
                    raise err from exc
                attempt += 1
                delay = self.backoff_s * 2 ** (attempt - 1)
                log.warning("Telescope: %s failed (%s); retry %d/%d in %.1fs", url, exc, attempt, self.retries, delay)
                self._sleep(delay)

    @staticmethod
    def _checked(body: dict, what: str) -> dict:
        code = int(body.get("ErrorNumber", 0) or 0)
        if code != 0:
            raise TelescopeError(f"{what} failed: {body.get('ErrorMessage') or 'Alpaca error'} (ErrorNumber {code})", code=code)
        return body

    def _api(self, device: str, number: int, name: str) -> str:
        return f"{self.base}/api/v1/{device}/{number}/{name}"

    # -- calls ---------------------------------------------------------------- #
    def get(self, device: str, number: int, name: str, *, retry: bool = True, **params: Any) -> Any:
        resp = self._request(
            "GET", self._api(device, number, name), timeout=self.timeout, retry=retry,
            params={**self._params(), **params},
        )
        return self._checked(_json(resp), f"{device}.{name}").get("Value")

    def put(self, device: str, number: int, name: str, *, retry: bool = True, **fields: Any) -> Any:
        resp = self._request(
            "PUT", self._api(device, number, name), timeout=self.timeout, retry=retry,
            data={**self._params(), **{k: _form(v) for k, v in fields.items()}},
        )
        return self._checked(_json(resp), f"{device}.{name}").get("Value")

    def management(self, name: str) -> Any:
        resp = self._request("GET", f"{self.base}/management/{name}", timeout=self.timeout, retry=True, params=self._params())
        return self._checked(_json(resp), f"management/{name}").get("Value")

    def image(self, number: int = 0) -> np.ndarray:
        """Download ``imagearray`` and return it as a 2-D array indexed ``[row, column]``.

        Asks for the binary ImageBytes format (fast) and falls back to JSON if the server
        answers with JSON.  Raises :class:`TelescopeError` for a reply it cannot interpret.
        """
        resp = self._request(
            "GET", self._api("camera", number, "imagearray"), timeout=max(self.timeout, IMAGE_TIMEOUT_S), retry=True,
            params=self._params(), headers={"Accept": _IMAGEBYTES_MIME},
        )
        if resp.headers.get("Content-Type", "").lower().startswith(_IMAGEBYTES_MIME):
            return parse_imagebytes(resp.content)
        body = self._checked(_json(resp), "camera.imagearray")
        return parse_json_image(body.get("Value"), self.get("camera", number, "cameraxsize"), self.get("camera", number, "cameraysize"))


def _json(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError as exc:
        raise TelescopeError(f"{resp.request.method} {resp.url}: reply was not JSON: {resp.text[:200]!r}") from exc
    if not isinstance(body, dict):
        raise TelescopeError(f"{resp.url}: unexpected reply {str(body)[:200]!r}")
    return body


def _form(value: Any) -> Any:
    """Alpaca form values: booleans are the words ``True`` / ``False``."""
    return ("True" if value else "False") if isinstance(value, bool) else value


def parse_imagebytes(blob: bytes) -> np.ndarray:
    """Decode an Alpaca ImageBytes body into ``[row, column]`` (the spec sends ``[X][Y]``, X slowest)."""
    if len(blob) < _IMAGEBYTES_HEADER.size:
        raise TelescopeError("ImageBytes reply is shorter than its header")
    (_ver, error, _client_tx, _server_tx, start, _image_type, transmit_type, rank, dim1, dim2, dim3) = _IMAGEBYTES_HEADER.unpack_from(blob)
    if error != 0:
        raise TelescopeError(f"camera.imagearray failed (ImageBytes error {error})", code=error)
    if rank != 2 or transmit_type not in _IMAGEBYTES_DTYPES:
        raise TelescopeError(f"unsupported ImageBytes image (rank {rank}, element type {transmit_type})")
    count = dim1 * dim2
    try:
        data = np.frombuffer(blob, dtype=_IMAGEBYTES_DTYPES[transmit_type], count=count, offset=start)
    except ValueError as exc:
        raise TelescopeError(f"ImageBytes body does not match its header ({dim1} x {dim2}): {exc}") from exc
    return np.ascontiguousarray(data.reshape(dim1, dim2).T)


def parse_json_image(value: Any, width: int | None, height: int | None) -> np.ndarray:
    """Turn a JSON ``imagearray`` into ``[row, column]``.

    A nested list follows the ASCOM standard, ``value[x][y]``, so it is transposed.  A flat list
    is read as ``height`` rows of ``width`` (the community's S30 Pro notes; UNCONFIRMED for the S50,
    so a first-light check must look at one frame).
    """
    arr = np.asarray(value)
    if arr.ndim == 2:
        return np.ascontiguousarray(arr.T)
    if arr.ndim == 1 and width and height and arr.size == int(width) * int(height):
        return arr.reshape(int(height), int(width))
    raise TelescopeError(f"cannot interpret the image array (shape {arr.shape}; camera reports {width} x {height})")


def to_uint16(image: np.ndarray) -> np.ndarray:
    """Clip a raw frame to the unsigned 16-bit range FITS consumers here expect."""
    if image.dtype == np.uint16:
        return image
    return np.clip(np.rint(image), 0, 65535).astype(np.uint16)


def parse_ascom_time(text: Any) -> datetime | None:
    """Parse an ASCOM ``yyyy-MM-ddTHH:mm:ss[.fff]`` UTC timestamp, or None if it is not one."""
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.rstrip("Z"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class AlpacaController(SeestarController):
    """The same controller API as :class:`SeestarController`, spoken over ASCOM Alpaca."""

    transport = "alpaca"

    def __init__(self, settings: Settings, mock: bool | None = None, sleep_fn: Callable[[float], None] = time.sleep):
        super().__init__(settings, mock=mock, sleep_fn=sleep_fn)
        self._alpaca: AlpacaClient | None = None
        self._unparked = False
        self._equatorial_system: int | None = None

    # -- lifecycle ------------------------------------------------------------ #
    def connect(self) -> None:
        if self.connected:
            return
        if self.mock:
            log.info("Telescope: MOCK mode -- no hardware connection opened")
            self.connected = True
            return
        s = self.settings
        client = AlpacaClient(
            s.seestar_host, s.seestar_alpaca_port, s.seestar_timeout_s,
            retries=s.seestar_retries, backoff_s=s.seestar_backoff_s, sleep_fn=self._sleep,
        )
        self._alpaca = client
        try:
            self._initialise()
        except TelescopeError:
            client.close()
            self._alpaca = None
            raise
        log.info("Telescope: connected over Alpaca to %s:%s", s.seestar_host, s.seestar_alpaca_port)
        self.connected = True

    def _initialise(self) -> None:
        client = self._require_alpaca()
        self._unparked = False
        state: dict[str, Any] = {"transport": "alpaca"}
        try:
            client.put("telescope", 0, "connected", Connected=True)
            client.put("camera", 0, "connected", Connected=True)
        except TelescopeError as exc:
            if exc.transient:
                raise
            raise TelescopeError(
                f"the Seestar's Alpaca server refused the connection ({exc}).  Check the scope is on, that the app "
                "is not holding it, and that the Alpaca switch under Settings, then Device management, is on; "
                "see docs/notes/firmware-and-authentication.md.",
                code=exc.code,
            ) from exc

        # Read-only facts for the hardware check and the log; every one is best-effort.
        for key, device, name in (
            ("telescope_name", "telescope", "name"),
            ("telescope_description", "telescope", "description"),
            ("driver_version", "telescope", "driverversion"),
            ("equatorial_system", "telescope", "equatorialsystem"),
            ("camera_name", "camera", "name"),
            ("camera_x", "camera", "cameraxsize"),
            ("camera_y", "camera", "cameraysize"),
            ("gain_min", "camera", "gainmin"),
            ("gain_max", "camera", "gainmax"),
        ):
            state[key] = self._read(client, device, name)
        try:
            state["configured_devices"] = client.management("v1/configureddevices")
        except TelescopeError as exc:
            if exc.transient:
                raise
            state["configured_devices"] = None
        self.device_state = state
        self.firmware = None  # Alpaca does not report the firmware build; read it in the Seestar app
        eq = state.get("equatorial_system")
        self._equatorial_system = int(eq) if isinstance(eq, (int, float)) else None
        log.info(
            "Telescope: %s, camera %s x %s, coordinates %s",
            state.get("telescope_name"), state.get("camera_x"), state.get("camera_y"),
            _EQUATORIAL_SYSTEMS.get(self._equatorial_system, "of unknown epoch"),
        )
        if self._equatorial_system not in (None, 2):
            log.warning(
                "Telescope: the mount reports coordinates in %s, not J2000; the commanded position may be off by up "
                "to about 0.4 degrees.  Confirm at first light (the pointing check will show it).",
                _EQUATORIAL_SYSTEMS.get(self._equatorial_system, "an unknown system"),
            )

        if self.settings.seestar_set_site:
            self._try(lambda: client.put("telescope", 0, "sitelatitude", SiteLatitude=self.settings.site_latitude_deg), "set site latitude")
            self._try(lambda: client.put("telescope", 0, "sitelongitude", SiteLongitude=self.settings.site_longitude_deg), "set site longitude")
            self._try(lambda: client.put("telescope", 0, "siteelevation", SiteElevation=self.settings.site_elevation_m), "set site elevation")
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            self._try(lambda: client.put("telescope", 0, "utcdate", UTCDate=now), "set clock")

    @staticmethod
    def _read(client: AlpacaClient, device: str, name: str) -> Any:
        try:
            return client.get(device, 0, name, retry=False)
        except TelescopeError as exc:
            if exc.transient:
                raise
            return None

    @staticmethod
    def _try(action: Callable[[], Any], what: str) -> None:
        try:
            action()
        except TelescopeError as exc:
            if exc.transient:
                raise
            log.warning("Telescope: could not %s (%s) -- continuing", what, exc)

    def disconnect(self) -> None:
        if self._alpaca is not None:
            self._alpaca.close()
            self._alpaca = None
        self.connected = False

    def _require_alpaca(self) -> AlpacaClient:
        if self._alpaca is None:
            raise TelescopeError("not connected to a Seestar")
        return self._alpaca

    # -- commands ------------------------------------------------------------- #
    def slew_to(self, target: dict, filter_name: str = "") -> None:
        ra, dec = float(target["ra_deg"]), float(target["dec_deg"])
        log.info("Telescope: slewing to %s (RA %.4f deg, Dec %.4f deg)", target["name"], ra, dec)
        self.pointing = {"name": target["name"], "ra_deg": ra, "dec_deg": dec}
        if self.mock:
            self._sleep(min(1.0, self.settings.mock_capture_seconds / 10.0))
            return
        client = self._require_alpaca()
        if not self._unparked:
            # Alpaca "unpark" is how the arm is raised.  UNCONFIRMED: it may need the app's first
            # initialisation of the session, in which case this is logged and the slew reports it.
            self._try(lambda: client.put("telescope", 0, "unpark"), "unpark (raise the arm)")
            self._unparked = True
        if filter_name:
            log.info("Telescope: filter %r is not selected over Alpaca (the position map is unconfirmed)", filter_name)
        client.put("telescope", 0, "slewtocoordinatesasync", RightAscension=ra / 15.0, Declination=dec)
        self._wait_until_slewed(ra, dec)
        # Tracking before the slew is refused with "below horizon" while the arm is still down.
        self._try(lambda: client.put("telescope", 0, "tracking", Tracking=True), "enable tracking")

    def _wait_until_slewed(self, ra_deg: float, dec_deg: float, timeout_s: float = SLEW_TIMEOUT_S) -> None:
        client = self._require_alpaca()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not client.get("telescope", 0, "slewing"):
                break
            self._sleep(2.0)
        else:
            raise TelescopeError("timed out waiting for the Seestar to finish slewing")
        try:
            cur_ra = float(client.get("telescope", 0, "rightascension")) * 15.0
            cur_dec = float(client.get("telescope", 0, "declination"))
        except (TelescopeError, TypeError, ValueError) as exc:
            log.warning("Telescope: could not read the position after the slew (%s)", exc)
            return
        dra = ((cur_ra - ra_deg + 180.0) % 360.0) - 180.0
        off = float(np.hypot(dra * np.cos(np.radians(dec_deg)), cur_dec - dec_deg))
        level = logging.WARNING if off > 0.5 else logging.INFO
        log.log(level, "Telescope: after the slew the mount reports %.3f deg from the commanded position", off)

    def configure(self, plan: dict) -> None:
        log.info(
            "Telescope: configure gain=%s exposure=%.1fs (filter %s not applied over Alpaca)",
            plan["gain"], plan["exposure_s"], plan.get("filter"),
        )
        if self.mock:
            return
        self._require_alpaca().put("camera", 0, "gain", Gain=int(plan["gain"]))

    def park(self) -> None:
        if self.mock or self._alpaca is None:
            return
        self._alpaca.put("telescope", 0, "park")

    def read_property(self, device: str, name: str, number: int = 0) -> Any:
        """Read one Alpaca property (device ``telescope``, ``camera`` or ``focuser``); for the hardware check."""
        return self._require_alpaca().get(device, number, name)

    def focuser_number(self) -> int:
        """Device number of the telephoto focuser, from the scope's own device list (or the setting)."""
        from tools.autofocus import find_focuser_number

        configured = (self.device_state or {}).get("configured_devices")
        return find_focuser_number(configured, self.settings.focuser_number)

    def focuser(self, target: dict | None = None):
        if self.mock:
            return super().focuser(target)
        from tools.autofocus import AlpacaFocuser

        return AlpacaFocuser(self._require_alpaca(), self.focuser_number(), sleep=self._sleep)

    def query(self, name: str, params: Any = None) -> Any:
        """Not available: the named JSON-RPC calls have no Alpaca equivalent (the hardware check has its own)."""
        raise TelescopeError(f"{name!r} is a JSON-RPC call and is not available over the Alpaca transport")

    def solve_pointing(self, timeout_s: float = 60.0) -> dict[str, Any] | None:
        """There is no solver call over Alpaca: always "not verified" (the frame is solved in software instead)."""
        return None

    # -- capture -------------------------------------------------------------- #
    def _capture_real(
        self, target: dict, plan: dict, out: Path, after_frame: Callable[[int, str], None] | None = None
    ) -> list[str]:
        client = self._require_alpaca()
        exposure = float(plan["exposure_s"])
        interval = float(plan.get("interval_s", 0.0))
        paths: list[str] = []
        for k in range(int(plan["count"])):
            self._raise_if_stopped()
            log.info("Telescope: exposing frame %d/%d (%.0f s)", k + 1, plan["count"], exposure)
            host_start = datetime.now(timezone.utc)
            client.put("camera", 0, "startexposure", Duration=exposure, Light=True)
            self._wait_for_image(client, exposure)
            device_start = parse_ascom_time(self._read(client, "camera", "lastexposurestarttime"))
            image = to_uint16(client.image())
            path = out / f"frame_{k + 1:03d}.fits"
            self._write_frame(path, image, target, plan, device_start or host_start, "device" if device_start else "host clock")
            paths.append(str(path))
            if after_frame is not None:
                after_frame(k, paths[-1])
            if interval > 0 and k < int(plan["count"]) - 1:
                self._idle(interval)
        return paths

    def _wait_for_image(self, client: AlpacaClient, exposure_s: float) -> None:
        deadline = time.monotonic() + exposure_s + READOUT_ALLOWANCE_S
        if exposure_s > 0:
            self._sleep(min(exposure_s, 5.0))  # no point polling while the shutter is plainly still open
        while time.monotonic() < deadline:
            if client.get("camera", 0, "imageready"):
                return
            self._sleep(1.0)
        raise TelescopeError(f"the camera did not report an image ready within {exposure_s + READOUT_ALLOWANCE_S:.0f} s of starting a {exposure_s:.0f} s exposure")

    def _write_frame(self, path: Path, image: np.ndarray, target: dict, plan: dict, start: datetime, time_source: str) -> None:
        hdr = fits.Header()
        hdr["OBJECT"] = target["name"]
        hdr["RA"] = float(target["ra_deg"])
        hdr["DEC"] = float(target["dec_deg"])
        hdr["EXPTIME"] = float(plan["exposure_s"])
        hdr["GAIN"] = int(plan["gain"])
        hdr["PIXSCALE"] = (nominal_pixel_scale_arcsec(image.shape), "[arcsec/px] nominal for this sensor size")
        hdr["DATE-OBS"] = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        hdr["TIMESRC"] = (time_source, "where DATE-OBS came from (UNCONFIRMED accuracy)")
        state = self.device_state or {}
        if state.get("camera_name"):
            hdr["INSTRUME"] = str(state["camera_name"])
        if state.get("telescope_name"):
            hdr["TELESCOP"] = str(state["telescope_name"])
        hdr["COMMENT"] = "Raw frame via ASCOM Alpaca; Bayer pattern and orientation UNCONFIRMED. No filter claimed."
        path.parent.mkdir(parents=True, exist_ok=True)
        fits.PrimaryHDU(data=image, header=hdr).writeto(path, overwrite=True)
