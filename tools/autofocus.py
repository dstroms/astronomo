"""Autofocus through the Seestar's ASCOM Alpaca focuser.

Why this exists
    The Seestar's own autofocus is a JSON-RPC call (``start_auto_focuse``).  Newer firmware wants a
    client-authentication handshake on that port, which Astronomo deliberately does not perform (see
    ``docs/notes/firmware-and-authentication.md``).  The same scope's Alpaca server lists a
    Focuser device, so this module drives it directly: take a short exposure, measure the star
    width, move, repeat across a window, fit the curve, and settle on its minimum.

What is and is not known
    The scope's Alpaca list does show a Focuser (the 2026-11-13 hardware check saved it).  Nothing
    else here has run on a physical Seestar: the step size and range, whether ``Move`` works while
    the scope is tracking, how long a move takes, and the backlash are all UNCONFIRMED.  The routine
    is built from the ASCOM Focuser specification and tested against a fake focuser.  It never
    moves outside the range the device reports, and it puts the focuser back where it started on any
    failure or interruption.

The measurement is injected (``measure``), so this module knows nothing about how a star width is
found: ``core.setup_checks`` supplies the same FWHM the first-frame focus check uses.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import numpy as np

from tools.telescope import StopRequested, TelescopeError

log = logging.getLogger(__name__)

#: A focus run that could not reach a better focus ends in one of these.
IMPROVED, NO_IMPROVEMENT, NO_MINIMUM, NO_STARS = "improved", "no_improvement", "no_minimum", "no_stars"

MOVE_TIMEOUT_S = 90.0
MIN_POINTS_FOR_FIT = 4


class FocusDevice(Protocol):
    """What the sweep needs from a focuser and the camera behind it."""

    def info(self) -> dict[str, Any]: ...
    def move_to(self, position: int) -> int: ...
    def frame(self, exposure_s: float, gain: int) -> np.ndarray: ...
    def halt(self) -> None: ...


# --------------------------------------------------------------------------- #
# The curve
# --------------------------------------------------------------------------- #
def window(centre: int, half_width: int, n_points: int, lo: int, hi: int) -> list[int]:
    """Ascending, distinct positions centred on ``centre`` and clamped to ``[lo, hi]``."""
    n_points = max(int(n_points), 3)
    raw = np.linspace(centre - half_width, centre + half_width, n_points)
    return sorted({int(min(max(round(float(v)), lo), hi)) for v in raw})


TARGET_TOLERANCE_PX = 0.15  # a start this close to the width wanted counts as there


def fit_focus_curve(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Minimum of star width against position: a parabola through width squared.

    Near focus a star's width follows a hyperbola, ``w^2 = w0^2 + k^2 (x - x0)^2``, so ``w^2`` is a
    parabola in the position ``x``.  Returns ``{"position", "fwhm_px", "inside", "rms_px", "n"}`` or
    None when too few samples measured a star or the parabola opens downward (no minimum).
    ``inside`` is whether the minimum lies between the first and last measured positions: a minimum
    outside the window is an extrapolation and is not trusted.
    """
    pts = [(float(s["position"]), float(s["fwhm_px"])) for s in samples if s.get("fwhm_px") is not None]
    if len(pts) < MIN_POINTS_FOR_FIT:
        return None
    x = np.array([p[0] for p in pts])
    w = np.array([p[1] for p in pts])
    centre, spread = float(x.mean()), max(float(np.ptp(x)), 1.0)
    u = (x - centre) / spread  # keeps the fit well conditioned whatever the step scale is
    a, b, c = np.polyfit(u, w**2, 2)
    if a <= 0.0:
        return None
    u0 = -b / (2.0 * a)
    w2_min = c - b * b / (4.0 * a)
    fwhm_min = math.sqrt(max(w2_min, 0.0))
    model = np.sqrt(np.clip(a * u * u + b * u + c, 0.0, None))
    return {
        "position": int(round(centre + u0 * spread)),
        "fwhm_px": fwhm_min,
        "inside": bool(x.min() <= centre + u0 * spread <= x.max()),
        "rms_px": float(np.sqrt(np.mean((model - w) ** 2))),
        "n": len(pts),
        "k2": float(a / (spread * spread)),  # w^2 = fwhm_px^2 + k2 (x - position)^2, x in focuser steps
    }


def position_for_width(fit: dict[str, Any], target_px: float, lo: float, hi: float) -> int | None:
    """The focuser position, on the low side of the minimum, where the fitted star width equals ``target_px``.

    None when the fit has no curvature to use or the target is not wider than the sharpest focus (then the
    sharpest focus is the answer).  The low side is the side the sweep approaches from, so backlash is the
    same as in the sweep.  The result is held to ``lo``..``hi``.
    """
    k2 = fit.get("k2")
    if not k2 or k2 <= 0.0 or target_px <= fit["fwhm_px"]:
        return None
    offset = math.sqrt((target_px**2 - fit["fwhm_px"] ** 2) / k2)
    return int(round(min(max(fit["position"] - offset, lo), hi)))


# --------------------------------------------------------------------------- #
# The routine
# --------------------------------------------------------------------------- #
def run_autofocus(
    device: FocusDevice,
    measure: Callable[[np.ndarray], dict[str, Any] | None],
    *,
    exposure_s: float = 3.0,
    gain: int = 80,
    n_points: int = 7,
    half_width: int | None = None,
    half_width_frac: float = 0.02,
    max_widenings: int = 2,
    min_improvement: float = 0.15,
    target_fwhm_px: float = 0.0,
    backlash_steps: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    settle_s: float = 1.0,
    stop_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Sweep, fit and settle.  Returns a report; on an error it tries to put the focuser back, then re-raises.

    ``measure(frame)`` returns ``{"fwhm_px", "n_stars"}`` or None when no star could be measured.
    The result is kept only when a confirming frame at the new position is at least ``min_improvement``
    (a fraction) sharper than the start; otherwise the focuser goes back to where it was.
    The re-raised error carries ``focuser_restored`` (whether the focuser is back at ``focuser_start``);
    when it is False the focuser is somewhere else and the operator has to set it.  ``stop_check()``
    is asked before every move; True raises ``StopRequested`` (and the focuser goes back).

    ``target_fwhm_px`` > 0 settles where the fitted curve reaches that width instead of at the sharpest
    focus (when the sharpest focus is narrower than it).  At 2.2 px a Seestar star is four bright pixels
    and aperture photometry depends on where the star falls; a slightly soft star is steadier.  Then the
    result is kept when the confirming frame is nearer the target than the start was, by the same
    ``min_improvement`` fraction.  0 takes the sharpest focus.
    """
    info = device.info()
    log.info("Focus: focuser %s", {k: v for k, v in info.items() if v is not None})
    start, lo, hi = int(info["position"]), 0, int(info["maxstep"])
    if hi <= lo:
        raise TelescopeError(f"the focuser reports a range of 0 to {hi}; cannot sweep it")
    if info.get("absolute") is False:
        raise TelescopeError("the focuser is not absolute; this routine needs absolute positions")
    hw = int(half_width) if half_width else max(1, int(round(half_width_frac * (hi - lo))))
    back = int(backlash_steps) if backlash_steps is not None else max(1, (2 * hw) // max(n_points - 1, 1))
    samples: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start_position": start, "range": [lo, hi], "half_width_steps": hw,
        "exposure_s": exposure_s, "gain": gain, "samples": samples,
        "unconfirmed": "Focuser step size, move behaviour and backlash have not been seen on a Seestar.",
    }

    log.info(
        "Focus: sweeping %d positions about %d (half-width %d steps of 0 to %d), %.0f s frames at gain %s",
        n_points, start, hw, hi, exposure_s, gain,
    )

    def sample(position: int) -> dict[str, Any]:
        if stop_check is not None and stop_check():
            raise StopRequested("stopped by the operator")
        actual = device.move_to(position)
        sleep(settle_s)
        found = measure(device.frame(exposure_s, gain))
        row = {
            "position": int(actual),
            "fwhm_px": None if found is None else float(found["fwhm_px"]),
            "n_stars": 0 if found is None else int(found.get("n_stars", 0)),
        }
        samples.append(row)
        log.info("Focus: position %d -> %s", row["position"],
                 "no star measured" if row["fwhm_px"] is None else f"FWHM {row['fwhm_px']:.2f} px ({row['n_stars']} stars)")
        return row

    try:
        before = sample(start)
        report["fwhm_before_px"] = before["fwhm_px"]
        centre, fit = start, None
        for widening in range(max_widenings + 1):
            for position in window(centre, hw, n_points, lo, hi):
                if not any(s["position"] == position for s in samples):
                    sample(position)
            measured = [s for s in samples if s["fwhm_px"] is not None]
            if len(measured) < MIN_POINTS_FOR_FIT:
                if widening >= max_widenings:
                    break
                centre = start  # nothing measured yet: widen around the start
                hw = min(hw * 2, hi - lo)
                continue
            fit = fit_focus_curve(samples)
            if fit is not None and fit["inside"]:
                break
            # No minimum inside the window: recentre on the sharpest sample and widen.
            sharpest = min(measured, key=lambda s: s["fwhm_px"])
            if widening >= max_widenings or sharpest["position"] in (lo, hi):
                break  # out of widenings, or the sharpest sample is at the end of the range
            centre = sharpest["position"]
            hw = min(hw * 2, hi - lo)
        report["fit"] = fit
        if not any(s["fwhm_px"] is not None for s in samples):
            report["status"] = NO_STARS
            report["detail"] = "No star could be measured at any position.  Point the scope at a star field and check the focus by eye."
            return _restore(device, report, start)
        if fit is None or not fit["inside"]:
            report["status"] = NO_MINIMUM
            report["detail"] = "No clear minimum in the star width across the positions tried."
            return _restore(device, report, start)
        # Approach the final position from below, the way the sweep did, so any backlash is the same.
        target = int(min(max(fit["position"], lo), hi))
        aim = float(target_fwhm_px) if target_fwhm_px and target_fwhm_px > 0 else 0.0
        softer = position_for_width(fit, aim, lo, hi) if aim else None
        if softer is not None:
            report["target_fwhm_px"], report["sharpest_position"] = aim, target
            log.info("Focus: sharpest focus is %d (FWHM %.2f px); settling at %d for a target width of %.1f px",
                     target, fit["fwhm_px"], softer, aim)
            target = softer
        device.move_to(max(target - back, lo))
        sleep(settle_s)
        final = sample(target)
        report["final_position"], report["fwhm_after_px"] = final["position"], final["fwhm_px"]
        before_w = report["fwhm_before_px"]
        if softer is not None and final["fwhm_px"] is not None and before_w is not None:
            # Already within TARGET_TOLERANCE_PX of the width wanted: a fit's own error is no reason to move.
            better = abs(before_w - aim) > TARGET_TOLERANCE_PX and (
                abs(final["fwhm_px"] - aim) <= abs(before_w - aim) * (1.0 - float(min_improvement))
            )
        else:
            better = final["fwhm_px"] is not None and (
                before_w is None or final["fwhm_px"] <= before_w * (1.0 - float(min_improvement))
            )
        if better:
            report["status"] = IMPROVED
            report["detail"] = (
                f"Focus moved from {start} to {final['position']}: FWHM "
                + (f"{before_w:.1f} -> {final['fwhm_px']:.1f} px." if before_w is not None else f"{final['fwhm_px']:.1f} px.")
            )
            return report
        report["status"] = NO_IMPROVEMENT
        report["detail"] = (
            "The fitted focus was not sharper than where the scope started "
            f"(FWHM {before_w if before_w is None else round(before_w, 2)} -> {final['fwhm_px'] if final['fwhm_px'] is None else round(final['fwhm_px'], 2)} px)."
        )
        return _restore(device, report, start)
    except BaseException as exc:
        # A failure, a stop or Ctrl-C: leave the focuser where it was found, then say what went wrong.
        exc.focuser_start = start  # type: ignore[attr-defined]
        exc.focuser_restored = return_to(device, start, sleep)  # type: ignore[attr-defined]
        raise


def return_to(device: FocusDevice, start: int, sleep: Callable[[float], None] = time.sleep, attempts: int = 3) -> bool:
    """Move the focuser back to ``start``, trying again a few times.  True only if it got there.

    The first real try (2026-11-16) was refused with ``SET_MOVE_FOCUSER ... internal error`` right after a
    Halt, so Halt is a last resort here, not the first step.  A failure is logged loudly with where the
    focuser is, because the operator has to set it by hand.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if device.move_to(start) == start:
                if attempt > 1:
                    log.info("Focus: the focuser is back at %d (attempt %d)", start, attempt)
                return True
            last = TelescopeError("the focuser stopped short of the start position")
        except Exception as err:  # noqa: BLE001 - keep trying; the original error is the one to report
            last = err
            log.warning("Focus: return to %d failed (attempt %d of %d): %s", start, attempt, attempts, err)
            if attempt == attempts - 1:
                try:
                    device.halt()  # in case it is still moving
                except Exception:  # noqa: BLE001
                    pass
        sleep(3.0)
    where = "an unknown position"
    try:
        where = str(device.info()["position"])
    except Exception:  # noqa: BLE001
        pass
    log.error(
        "Focus: THE FOCUSER WAS NOT RETURNED.  It started at %d and is now at %s (%s).  Set it back in the Seestar "
        "app, or run python main.py --focus again to find focus.", start, where, last,
    )
    return False


def _restore(device: FocusDevice, report: dict[str, Any], start: int) -> dict[str, Any]:
    report["final_position"] = int(device.move_to(start))
    report.setdefault("fwhm_after_px", report.get("fwhm_before_px"))
    return report


# --------------------------------------------------------------------------- #
# Alpaca focuser
# --------------------------------------------------------------------------- #
def find_focuser_number(configured: Any, preferred: int = -1) -> int:
    """Device number of the telephoto focuser in an Alpaca ``configureddevices`` list.

    The S50 Pro's list carries a Telephoto and a Wide Angle camera and focuser (the Wide Angle ones
    are device 1).  ``preferred`` (>= 0) wins; otherwise the focuser whose name says Telephoto, else
    the only focuser, else device 0.
    """
    if preferred >= 0:
        return int(preferred)
    focusers = [d for d in (configured or []) if str(d.get("DeviceType", "")).lower() == "focuser"]
    for d in focusers:
        if "telephoto" in str(d.get("DeviceName", "")).lower():
            return int(d.get("DeviceNumber", 0))
    if len(focusers) == 1:
        return int(focusers[0].get("DeviceNumber", 0))
    if not focusers:
        raise TelescopeError("the Seestar's Alpaca server lists no focuser, so the focus cannot be moved from here")
    return 0


class AlpacaFocuser:
    """The focuser and the telephoto camera of one Seestar, over the Alpaca REST client."""

    def __init__(
        self, client, number: int, *, camera_number: int = 0, sleep: Callable[[float], None] = time.sleep,
        move_timeout_s: float = MOVE_TIMEOUT_S,
    ):
        self.client, self.number, self.camera_number = client, int(number), int(camera_number)
        self._sleep, self.move_timeout_s = sleep, move_timeout_s
        self._info: dict[str, Any] | None = None

    def _get(self, name: str) -> Any:
        return self.client.get("focuser", self.number, name)

    def connect(self) -> None:
        self.client.put("focuser", self.number, "connected", Connected=True)

    def info(self) -> dict[str, Any]:
        self.connect()
        out: dict[str, Any] = {"number": self.number}
        for key, prop in (("position", "position"), ("maxstep", "maxstep"), ("maxincrement", "maxincrement"),
                          ("absolute", "absolute"), ("stepsize", "stepsize"), ("temperature", "temperature"),
                          ("tempcomp", "tempcomp")):
            try:
                out[key] = self._get(prop)
            except TelescopeError as exc:
                if exc.transient:
                    raise
                out[key] = None  # ASCOM lets a driver refuse a property it does not have
        if out.get("position") is None or out.get("maxstep") is None:
            raise TelescopeError(f"the focuser (device {self.number}) did not report its position and range: {out}")
        self._info = out
        return out

    def move_to(self, position: int) -> int:
        info = self._info or self.info()
        position = int(position)
        if not 0 <= position <= int(info["maxstep"]):
            raise TelescopeError(f"refusing to move the focuser to {position}: its range is 0 to {info['maxstep']}")
        cur = int(self._get("position"))
        limit = int(info.get("maxincrement") or 0) or abs(position - cur) or 1
        while cur != position:
            step = max(min(position, cur + limit), cur - limit)  # at most ``maxincrement`` per Move
            self.client.put("focuser", self.number, "move", Position=int(step))
            self._wait_until_still()
            now = int(self._get("position"))
            if now == cur:
                raise TelescopeError(f"the focuser did not move: asked for {step}, still at {now}")
            cur = now
            if abs(cur - step) > max(2, int(0.01 * int(info["maxstep"]))):
                log.warning("Focus: asked for %d, the focuser stopped at %d", step, cur)
                break
        return cur

    def _wait_until_still(self) -> None:
        deadline = time.monotonic() + self.move_timeout_s
        while time.monotonic() < deadline:
            if not self._get("ismoving"):
                return
            self._sleep(0.5)
        raise TelescopeError(f"the focuser was still moving after {self.move_timeout_s:.0f} s")

    def halt(self) -> None:
        try:
            self.client.put("focuser", self.number, "halt")
        except TelescopeError:
            pass  # not every driver implements Halt; the caller moves back next

    def frame(self, exposure_s: float, gain: int) -> np.ndarray:
        from tools.seestar_alpaca import READOUT_ALLOWANCE_S, to_uint16

        cam = self.camera_number
        self.client.put("camera", cam, "gain", Gain=int(gain))
        self.client.put("camera", cam, "startexposure", Duration=float(exposure_s), Light=True)
        deadline = time.monotonic() + exposure_s + READOUT_ALLOWANCE_S
        self._sleep(min(exposure_s, 5.0))
        while time.monotonic() < deadline:
            if self.client.get("camera", cam, "imageready"):
                return to_uint16(self.client.image(cam))
            self._sleep(0.5)
        raise TelescopeError("the camera did not report a focus frame in time")


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
class MockFocuser:
    """A focuser with a hidden best position, behind the mock sky.  Star width grows away from it.

    Shaped like the hyperbola the routine fits, so a simulated run proves the logic and says nothing
    about the real focuser's step size or backlash.
    """

    def __init__(self, target: dict, *, best: int = 1180, start: int = 1000, maxstep: int = 2600,
                 min_fwhm_px: float = 3.0, px_per_step: float = 0.012, backlash: int = 0):
        from tools.mock_sky import MockSky

        self.sky = MockSky.from_target(target)
        self.best, self.position, self.maxstep = int(best), int(start), int(maxstep)
        self.min_fwhm_px, self.px_per_step, self.backlash = min_fwhm_px, px_per_step, int(backlash)
        self.moves: list[int] = []
        self._frame_index = 0

    def fwhm_at(self, position: int) -> float:
        return math.hypot(self.min_fwhm_px, self.px_per_step * (position - self.best))

    def info(self) -> dict[str, Any]:
        return {"position": self.position, "maxstep": self.maxstep, "absolute": True, "maxincrement": self.maxstep,
                "stepsize": None, "temperature": None, "tempcomp": False, "number": 0}

    def move_to(self, position: int) -> int:
        if not 0 <= int(position) <= self.maxstep:
            raise TelescopeError(f"mock focuser: {position} is outside 0 to {self.maxstep}")
        self.moves.append(int(position))
        self.position = int(position) + (self.backlash if int(position) < self.position else 0)
        return self.position

    def halt(self) -> None:
        return None

    def frame(self, exposure_s: float, gain: int) -> np.ndarray:
        self._frame_index += 1
        sky = replace(self.sky, fwhm_px=self.fwhm_at(self.position))
        return sky.render(self.sky.epoch0, exposure_s, self._frame_index)
