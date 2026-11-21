"""Exoplanet-transit experiment plugin.

Science case: point at a star known to host a transiting planet, take a long, evenly spaced
sequence of frames across the predicted transit, build a differential light curve, fit a transit,
and ask whether the measured depth, duration and mid-time agree with the catalogue ephemeris.

Everything transit-specific lives in this package; Astronomo core only talks to the
:class:`~core.base_plugin.BaseExperimentPlugin` interface.

Differences from the asteroid plugin worth knowing about
--------------------------------------------------------
* One observation is **hours long** (hundreds of frames in live mode), not seconds.
* The graph runs targets back to back and does **not** enforce ``window_start``/``window_end``.
  Live targets are therefore chosen so their windows do not overlap and are ordered by start time,
  but you must start the mission when the first window opens (the log prints it).
* ``analyze_frames`` only receives file paths, so the photometry is a **blind** search; comparing
  with the expected ephemeris happens in :meth:`cross_reference`, which does see the target.

Data source
-----------
Frames come from the Seestar (or the mock telescope).  This plugin has no archive replay.

Target selection (live mode) and outputs
----------------------------------------
Targets come from the Pro-Am networks: ExoClock and the NASA Exoplanet Watch list, merged and ranked by how stale each
planet's published ephemeris is, with a boost for ExoClock's priority and the NASA Exoplanet Archive's TTV flag
(:mod:`plugins.transit_finder.networks`).  ``ASTRONOMO_TRANSIT_TARGETS`` replaces that with an operator's own list;
if no list can be read at all a short built-in list is used and labelled ``manual``.  The default output is one
AAVSO Exoplanet Report per planet (:meth:`TransitFinderPlugin.build_submissions`); timing offsets, missing transits
and unexpected dips are proposed as anomalies (:meth:`TransitFinderPlugin.detect_anomalies`), which the mission log
flags for a person to review.

Environment overrides (all optional)
    ASTRONOMO_TRANSIT_TARGETS         comma list of NASA Exoplanet Archive planet names; replaces the network lists (live mode)
    ASTRONOMO_TRANSIT_MIN_ALT_DEG     minimum target altitude across the window (default 30)
    ASTRONOMO_TRANSIT_LOOKAHEAD_H     how far ahead to look for transits (default 24)
    ASTRONOMO_TRANSIT_BASELINE_MIN    out-of-transit baseline to take before and after (default 60)
    ASTRONOMO_TRANSIT_MAX_VMAG        faintest host V magnitude to attempt (default 13.0)
    ASTRONOMO_TRANSIT_MIN_DEPTH_PCT   shallowest catalogue depth to attempt (default 0.5)
    ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN assumed timing error of one night, used only by the dashboard's impact estimate (default 3.0)
    ASTRONOMO_TRANSIT_FRAMES          frames per target (default 30 simulated / 480 live)
    ASTRONOMO_TRANSIT_EXPOSURE_S      exposure per frame (default 10)
    ASTRONOMO_TRANSIT_INTERVAL_S      extra delay between frames (default 470 simulated / 20 live)
"""

from __future__ import annotations

import logging
import math
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from config import Settings
from core import interlocks
from core.base_plugin import BaseExperimentPlugin
from core.networks import TASK_MANUAL, NetworkError
from core.submission import SubmissionDraft
from plugins.transit_finder import archive, networks, photometry
from plugins.transit_finder import opportunities as opp
from plugins.transit_finder.scene import TransitScene
from tools.astrometry import (
    SEESTAR_S50_PIXEL_SCALE_ARCSEC,
    angular_separation_arcsec,
    bjd_tdb_to_utc,
    estimate_translation,
    pixel_scale_arcsec,
    shift_wcs,
    sun_altitude_deg,
    target_altaz_many,
    utc_to_bjd_tdb_many,
    wcs_from_dict,
)
from tools import upload_formats
from tools.catalogs import CatalogClient, CatalogError

log = logging.getLogger(__name__)

# Used only when no Pro-Am target list can be read: bright, deep, short-period hot Jupiters, forgiving for a 50 mm telescope.
DEFAULT_FALLBACK_TARGETS = "WASP-12 b,HAT-P-32 b,TrES-3 b,XO-1 b"
GAIA_G_LIMIT = 16.0
IDENTITY_RADIUS_ARCSEC = 10.0  # measured target star vs catalogue position, after Gaia calibration
TIMING_TOLERANCE_MIN = 20.0  # floor on the mid-time agreement window (catalogue ephemerides drift)
DEPTH_TOLERANCE = 0.25  # fractional floor on the depth agreement window
DURATION_TOLERANCE = 0.30  # fractional duration agreement window
SIM_WINDOW_FRACTION = 0.5  # simulated transit lasts at most this fraction of the observing span


def _env(name: str, default: str) -> str:
    return os.environ.get(f"ASTRONOMO_TRANSIT_{name}", default)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TransitFinderPlugin(BaseExperimentPlugin):
    name = "transit_finder"
    description = "Measures exoplanet transits with differential photometry and compares them with the NASA Exoplanet Archive."


    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.simulated = settings.offline_services

    # ------------------------------------------------------------------ #
    # 1. Targets
    # ------------------------------------------------------------------ #
    def generate_targets(self) -> list[dict]:
        if self.settings.offline_services:
            return self._simulated_targets()
        return self._live_targets()

    def _simulated_targets(self) -> list[dict]:
        """A known planet in transit, plus a star with no transit as a control."""
        plan = self.get_exposure_plan()
        cadence = plan["exposure_s"] + plan["interval_s"]
        span = (plan["count"] - 1) * cadence + plan["exposure_s"]  # first frame start -> last frame end
        now = datetime.now(timezone.utc).replace(microsecond=0)
        window = {"window_start": _iso(now), "window_end": _iso(now + timedelta(seconds=span))}
        t_mid = span / 2.0
        duration = min(7200.0, SIM_WINDOW_FRACTION * span)
        systematics = {
            "drift_px_per_hr": [6.0, -4.0],
            "transparency_trend_per_hr": -0.03,  # airmass-like dimming, common to every star
            "transparency_jitter": 0.004,  # thin cloud, common to every star
        }

        def host(perr: tuple[float, float], mag: float, depth: float) -> dict:
            # The star sits at its catalogue position; the mount lands perr arcsec away, so on the
            # detector the star is offset from the centre by the opposite amount (north up, east left).
            scale = SEESTAR_S50_PIXEL_SCALE_ARCSEC
            return {
                "dx_px": perr[0] / scale, "dy_px": -perr[1] / scale, "mag": mag, "depth": depth,
                "t_mid_s": t_mid, "duration_s": duration, "ingress_s": 0.125 * duration,
                "baseline_slope_per_hr": -0.002,  # differential extinction: affects the target only
            }

        perr_a, perr_b = (28.0, -17.0), (-35.0, 22.0)
        return [
            {
                "name": "WASP-12 b (sim)", "designation": "WASP-12 b", "planet": "WASP-12 b", "host": "WASP-12",
                "ra_deg": 97.6366, "dec_deg": 29.6723, "priority": 2.0, **window, "v_mag": 11.4,
                # What the "archive" claims: a mid-time 4 min late, a slightly deeper and longer transit.
                "predicted_t_mid_iso": _iso(now + timedelta(seconds=t_mid + 240.0)),
                "predicted_t_mid_err_min": 5.0,
                "duration_h": duration / 3600.0 + 0.1, "depth_pct": 1.5, "period_d": 1.0914,
                "distance_pc": 427.2,  # what the archive lists for the real WASP-12; simulated runs have no Gaia parallax
                "sim": {"seed": 12, "epoch0_iso": _iso(now), "pointing_error_arcsec": list(perr_a),
                        "transit": host(perr_a, 11.4, 0.014), **systematics},
            },
            {
                "name": "Control star (sim)", "designation": None, "planet": None, "host": "control",
                "ra_deg": 200.0, "dec_deg": 35.0, "priority": 1.0, **window, "v_mag": 11.9,
                "predicted_t_mid_iso": None,  # no transit expected in this window
                "sim": {"seed": 6, "epoch0_iso": _iso(now), "pointing_error_arcsec": list(perr_b),
                        "transit": host(perr_b, 11.9, 0.0), **systematics},
            },
        ]

    def mock_scene(self, target: dict) -> TransitScene:
        return TransitScene.from_target(target)

    # -- live targets: Pro-Am network lists first ------------------------- #
    def _live_targets(self) -> list[dict]:
        """Upcoming, observable transits, chosen from the Pro-Am networks' lists of what most needs timing.

        Order of precedence:

        1. ``ASTRONOMO_TRANSIT_TARGETS`` (an operator's explicit list of NASA Exoplanet Archive names).
        2. The merged ExoClock / Exoplanet Watch / NASA TTV lists, ranked by how stale each planet's
           ephemeris is (see :mod:`plugins.transit_finder.networks`).
        3. If no network list can be read at all (and there is no cached copy), a short built-in list of
           bright hot Jupiters, tagged ``manual`` so the mission log says the networks were not consulted.
        """
        from astropy.time import Time

        now = datetime.now(timezone.utc).replace(microsecond=0)
        jd_now = float(Time(now).jd)
        explicit = [n.strip() for n in _env("TARGETS", "").split(",") if n.strip()]
        info: dict[str, Any] = {}
        if explicit:
            rows = self._manual_rows(explicit, "operator", "chosen by the operator (ASTRONOMO_TRANSIT_TARGETS)")
            ranked = False
        else:
            try:
                rows, info = networks.fetch_candidates(
                    self.settings.archive_cache_dir, self.settings.network_cache_ttl_h * 3600.0, jd_now
                )
                ranked = True
            except NetworkError as exc:
                log.warning(
                    "No Pro-Am target list could be read (%s). Falling back to the built-in bright-planet list; "
                    "this mission is NOT following a network's priorities.", exc,
                )
                rows = self._manual_rows(
                    DEFAULT_FALLBACK_TARGETS.split(","), "built_in",
                    "built-in bright hot Jupiter; no network target list was reachable",
                )
                ranked = False
        return self._schedule(rows, now, jd_now, info, ranked=ranked)

    # -- opportunities: what a person could take part in (the dashboard's Opportunities page) ------ #
    def opportunities(self, now: datetime | None = None, *, days: int = opp.HORIZON_DAYS, refresh: bool = False) -> dict:
        """Planets the networks want timed, which this telescope can reach, when, and what a timing would be worth."""
        return opp.build_report(self.settings, now=now, days=days, refresh=refresh)

    def planned_target(self, opportunity: dict, window: dict) -> dict:
        return opp.planned_target(opportunity, window)

    def recheck_planned_target(self, target: dict) -> dict:
        return opp.recheck_planned(self.settings, target)

    def _manual_rows(self, names: list[str], label: str, reason: str) -> list[dict]:
        """Planets named by a person (or the built-in list) rather than by a network, from the NASA Exoplanet Archive."""
        rows = archive.fetch_planets(names)
        if not rows:
            raise CatalogError(f"the NASA Exoplanet Archive returned no usable ephemeris for: {', '.join(names)}")
        return [
            {**r, "sources": [label], "task": TASK_MANUAL, "reason": reason, "score": None, "terms": {},
             "ephemeris_source": "nasa_exoplanet_archive"}
            for r in rows
        ]

    def _schedule(self, rows: list[dict], now: datetime, jd_now: float, info: dict, *, ranked: bool) -> list[dict]:
        """Turn candidate planets into non-overlapping observable transit windows within the look-ahead."""
        s = self.settings
        limits = opp.Limits.from_env(s)  # the same limits the dashboard's Opportunities page uses
        min_alt, max_v, min_depth = limits.min_alt_deg, limits.max_vmag, limits.min_depth_pct
        lookahead = timedelta(hours=float(_env("LOOKAHEAD_H", "24")))
        baseline = timedelta(minutes=limits.baseline_min)

        events: list[dict] = []
        for row in rows:
            if row["v_mag"] is None and ranked:  # a network list is wide: unknown brightness is not "suitable"
                continue
            if row["v_mag"] is not None and row["v_mag"] > max_v:
                log.debug("Skipping %s: V=%.1f fainter than %.1f", row["planet"], row["v_mag"], max_v)
                continue
            if row["depth_pct"] is None and ranked:
                continue
            if row["depth_pct"] is not None and row["depth_pct"] < min_depth:
                log.debug("Skipping %s: depth %.2f%% below %.2f%%", row["planet"], row["depth_pct"], min_depth)
                continue
            half = timedelta(hours=row["duration_h"] / 2.0)
            n0 = math.floor((jd_now - row["t0_bjd_tdb"]) / row["period_d"]) - 1
            for n in range(n0, n0 + int(lookahead.total_seconds() / 86400.0 / row["period_d"]) + 4):
                bjd = row["t0_bjd_tdb"] + n * row["period_d"]
                # Cheap pre-filter on a time that ignores the (< 9 min) barycentric correction, so the
                # exact conversion is only done for epochs that can matter.
                rough = datetime.fromtimestamp((bjd - 2440587.5) * 86400.0, tz=timezone.utc)
                rough_start = rough - half - baseline
                if rough_start < now - timedelta(minutes=30) or rough_start > now + lookahead + timedelta(minutes=30):
                    continue
                mid = bjd_tdb_to_utc(bjd, row["ra_deg"], row["dec_deg"], s.site_latitude_deg, s.site_longitude_deg, s.site_elevation_m)
                start, end = mid - half - baseline, mid + half + baseline
                if start < now - timedelta(minutes=15) or start > now + lookahead:
                    continue
                if not self._observable(row, start, mid, end, min_alt):
                    continue
                err_d = networks.propagated_uncertainty_d(n, row.get("period_err_d"), row["t0_err_d"])
                events.append({
                    **row, "mid": mid, "start": start, "end": end, "n": n,
                    "err_min": None if math.isinf(err_d) else err_d * 1440.0,
                })

        # Targets are observed back to back and the graph does not wait for windows: keep a non-overlapping
        # set, best first (a network's priority, else the deeper transit, wins a clash), then order by start.
        events.sort(key=lambda e: -(e["score"] if ranked else (e["depth_pct"] or 0.0)))
        chosen: list[dict] = []
        for ev in events:
            if len(chosen) >= self.target_limit():
                break
            if all(ev["end"] + timedelta(minutes=15) <= c["start"] or ev["start"] >= c["end"] + timedelta(minutes=15) for c in chosen):
                chosen.append(ev)
        chosen.sort(key=lambda e: e["start"])

        targets = []
        for ev in chosen:
            hours_to_start = (ev["start"] - now).total_seconds() / 3600.0
            net = networks.network_block_for(ev, info)
            targets.append({
                "name": ev["planet"], "designation": ev["planet"], "planet": ev["planet"], "host": ev["host"],
                "ra_deg": ev["ra_deg"], "dec_deg": ev["dec_deg"],
                "priority": 1000.0 - hours_to_start,  # sooner first
                "window_start": _iso(ev["start"]), "window_end": _iso(ev["end"]),
                "v_mag": ev["v_mag"], "period_d": ev["period_d"], "period_err_d": ev.get("period_err_d"),
                "duration_h": ev["duration_h"], "depth_pct": ev["depth_pct"],
                "distance_pc": ev.get("distance_pc"),
                "predicted_t_mid_iso": _iso(ev["mid"]),
                "predicted_t_mid_err_min": ev["err_min"] if ev["err_min"] is not None else 60.0,
                "epoch_number": ev["n"], "ephemeris_ref": ev.get("ephemeris_source"),
                "network": net,
            })
            log.info(
                "Transit of %s [%s]: mid %s UTC (+/- %s min), window %s to %s UTC, depth %s%%. %s",
                ev["planet"], net["task"], _iso(ev["mid"])[:16],
                "?" if ev["err_min"] is None else f"{ev['err_min']:.1f}", _iso(ev["start"])[11:16], _iso(ev["end"])[11:16],
                ev["depth_pct"], ev["reason"],
            )
        if not targets:
            raise CatalogError(
                f"no observable transit among {len(rows)} candidate planet(s) in the next {lookahead.total_seconds() / 3600:.0f} h "
                f"(altitude > {min_alt:.0f} deg, dark, V <= {max_v:g}, depth >= {min_depth:g}%). "
                "Raise ASTRONOMO_TRANSIT_LOOKAHEAD_H or relax the limits."
            )
        log.info(
            "The first observing window opens at %s UTC. A mission on a real telescope waits for it; start it before then.",
            targets[0]["window_start"][:16],
        )
        return targets

    def _observable(self, row: dict, start: datetime, mid: datetime, end: datetime, min_alt: float) -> bool:
        s = self.settings
        limits = interlocks.SiteLimits(min_altitude_deg=min_alt, horizon=tuple(s.site_horizon))
        for when in (start, mid, end):
            alt, az = target_altaz_many([row["ra_deg"]], [row["dec_deg"]], s.site_latitude_deg, s.site_longitude_deg, s.site_elevation_m, [when])
            if alt[0] < interlocks.minimum_altitude(float(az[0]), limits):  # the site's horizon, where it is higher than the minimum
                return False
            if not s.mock_mode and sun_altitude_deg(s.site_latitude_deg, s.site_longitude_deg, s.site_elevation_m, when) > s.max_sun_altitude_deg:
                return False
        return True

    # ------------------------------------------------------------------ #
    # 2. Exposure plan
    # ------------------------------------------------------------------ #
    def get_exposure_plan(self) -> dict:
        live = not self.settings.offline_services
        return {
            "gain": 80,
            "exposure_s": float(_env("EXPOSURE_S", "10")),
            "filter": "IRCUT",
            "count": int(_env("FRAMES", "480" if live else "30")),
            # Live: ~30 s cadence over ~4 h.  Simulated: 8 min cadence on the *simulated* clock.
            "interval_s": float(_env("INTERVAL_S", "20" if live else "470")),
        }

    # ------------------------------------------------------------------ #
    # 3. Reduction
    # ------------------------------------------------------------------ #
    def analyze_frames(self, fits_paths: list[str]) -> dict:
        if not fits_paths:
            raise ValueError("analyze_frames() received no FITS files")
        figure_dir = Path(fits_paths[0]).parent
        findings = photometry.analyze(fits_paths, None, figure_dir=figure_dir)
        findings["plugin"] = self.name
        photo = findings.get("photometry") or {}
        if photo.get("target_xy") and photo.get("comparison_xy") is not None:
            try:
                findings["figures"].append(
                    photometry.render_finder(
                        fits_paths[0], photo["target_xy"], photo["comparison_xy"], figure_dir / "finder.png",
                        name=findings.get("object") or "Target",
                        aperture_radius_px=float(photo.get("aperture_radius_px") or 5.0),
                    )
                )
            except Exception:  # noqa: BLE001 - a missing chart must not discard the light curve
                log.exception("Transit: finder chart failed")
                findings.setdefault("warnings", []).append("The field image of the target and comparison stars was not written; see logs.")
        return findings

    # ------------------------------------------------------------------ #
    # 4. Cross-reference
    # ------------------------------------------------------------------ #
    def describe_hypothesis(self, targets: list[dict]) -> str:
        names = ", ".join(t["name"] for t in targets)
        return (
            "A planet that passes in front of its star blocks a small fraction of the light for a few hours. "
            f"We observe {len(targets)} field(s) ({names}) with a long sequence of frames, measure each target's "
            "brightness relative to nearby comparison stars, and test whether a transit is present and whether "
            "its depth, duration and mid-time agree with the catalogue ephemeris."
        )

    def llm_guidance(self) -> str:
        return (
            "Domain: exoplanet transit photometry with a 50 mm smart telescope. A 'transit candidate' is a dip fitted "
            "by a trapezoid model; it is never a confirmed planet. If 'significant' is false or n_transits_detected is "
            "0, no transit was detected: do not describe the best-fit dip as a transit. A dip at the catalogue "
            "ephemeris with consistent depth is a re-detection of a known planet, not a discovery. Depth is "
            "differential and instrumental; blends and eclipsing binaries are not excluded. 'snr' is the depth "
            "divided by its error; delta_bic compares against a no-transit baseline."
        )

    def summarize_for_llm(self, findings: dict, crossref: dict | None = None, target: dict | None = None) -> dict:
        """Concise JSON: photometry summary plus the comparison with the ephemeris.  No light curve, no pixels."""
        base = findings.get("agent_summary") or photometry.summarize(findings)
        out: dict[str, Any] = {"target": (target or {}).get("name"), **base}
        xref = crossref or {}
        exp = xref.get("expected")
        if exp:
            out["catalogue"] = {k: exp.get(k) for k in ("depth_pct", "duration_h", "t_mid_err_min") if exp.get(k) is not None}
        else:
            out["catalogue"] = "no transit expected in this window"
        ident = xref.get("identity") or {}
        if ident.get("status"):
            out["target_identity"] = ident["status"]
        for entry in (xref.get("entries") or [])[:1]:
            out["verdict"] = {
                k: (round(entry[k], 2) if isinstance(entry.get(k), float) else entry.get(k))
                for k in ("classification", "t_mid_residual_min", "depth_ratio", "duration_ratio") if entry.get(k) is not None
            }
        nd = xref.get("non_detection") or {}
        if nd.get("depth_upper_limit_3sigma_pct") is not None:
            out["depth_upper_limit_3sigma_pct"] = round(nd["depth_upper_limit_3sigma_pct"], 3)
        return out

    @staticmethod
    def _expected(target: dict) -> dict | None:
        if not target.get("predicted_t_mid_iso"):
            return None
        return {
            "planet": target.get("planet") or target.get("designation") or target.get("name"),
            "t_mid_iso": target["predicted_t_mid_iso"],
            "t_mid_err_min": float(target.get("predicted_t_mid_err_min") or 10.0),
            "duration_h": target.get("duration_h"),
            "depth_pct": target.get("depth_pct"),
            "distance_pc": target.get("distance_pc"),
        }

    def cross_reference(self, findings: dict, catalogs: CatalogClient, target: dict) -> dict:
        detections = findings.get("detections") or []
        expected = self._expected(target)
        result: dict[str, Any] = {
            "summary": "", "entries": [], "services": {}, "calibration": None, "identity": None,
            "expected": expected, "non_detection": None, "notes": [],
        }
        calib, identity = self._check_identity(findings, catalogs, target)
        result["calibration"], result["identity"] = calib, identity
        result["services"]["gaia_dr3"] = calib.get("source") or calib["status"]
        result["services"]["exoplanet_ephemeris"] = (
            "simulated" if self.simulated else "nasa-exoplanet-archive (via target)"
        ) if expected else "none (no transit expected)"
        if identity["status"] == "mismatch":
            result["notes"].append(
                f"The measured target star is {identity['separation_arcsec']:.0f} arcsec from the catalogue position "
                "of the host: the wrong star may have been measured."
            )

        result["distance"] = self._distance(target.get("distance_pc"), identity)
        if result["distance"].get("note"):
            result["notes"].append(result["distance"]["note"])

        for det in detections:
            result["entries"].append(self._classify(det, expected, identity))

        if detections:
            e = result["entries"][0]
            result["summary"] = (
                f"Transit candidate {e['classification'].replace('_', ' ')}"
                + (f" (mid-time {e['t_mid_residual_min']:+.1f} min from the ephemeris, depth {e['depth_ratio']:.2f}x catalogue)."
                   if e.get("t_mid_residual_min") is not None and e.get("depth_ratio") is not None else ".")
            )
            if e["classification"] == "unexpected_dip":
                result["notes"].append(
                    "A dip with no catalogue transit expected is not evidence of a new planet: eclipsing binaries, "
                    "blends, clouds and instrumental systematics produce dips too."
                )
        else:
            result["non_detection"] = self._non_detection(findings, expected)
            result["summary"] = result["non_detection"]["text"]
        return result

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _distance(archive_pc: float | None, identity: dict) -> dict[str, Any]:
        """Host-star distance from the Exoplanet Archive and from the Gaia DR3 parallax of the measured star.

        The Gaia value is only used when the measured star was confirmed to be the host and the parallax is
        significant (S/N >= 5, so that inverting it is reasonable); it is the plain inverse, with no
        zero-point correction.  The two are flagged as inconsistent when they differ by more than 10 %.
        """
        out: dict[str, Any] = {"archive_pc": archive_pc, "gaia_pc": None, "gaia_parallax_mas": None,
                               "gaia_parallax_err_mas": None, "consistent": None, "note": None}
        plx, err = identity.get("nearest_gaia_parallax_mas"), identity.get("nearest_gaia_parallax_err_mas")
        if identity.get("status") != "confirmed" or plx is None or err is None or plx <= 0 or plx / max(err, 1e-9) < 5.0:
            return out
        out.update(gaia_parallax_mas=plx, gaia_parallax_err_mas=err, gaia_pc=1000.0 / plx)
        if archive_pc:
            out["consistent"] = abs(out["gaia_pc"] - archive_pc) <= 0.1 * archive_pc
            if not out["consistent"]:
                out["note"] = (
                    f"The Gaia DR3 parallax of the measured star implies {out['gaia_pc']:.0f} pc but the archive gives "
                    f"{archive_pc:.0f} pc: check that the right star was measured."
                )
        return out

    def _classify(self, det: dict, expected: dict | None, identity: dict) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "detection_id": det["id"], "classification": "unexpected_dip", "match": None, "notes": [],
            "calibrated": identity["status"] != "unverified",
        }
        if identity["status"] == "mismatch":
            entry["classification"] = "target_identity_mismatch"
            return entry
        if expected is None:
            entry["notes"].append("No transit was expected in this window.")
            return entry

        res = (_parse(det["epoch_mid_iso"]) - _parse(expected["t_mid_iso"])).total_seconds() / 60.0
        sigma = math.hypot(det.get("t_mid_err_min") or 0.0, expected["t_mid_err_min"])
        entry["t_mid_residual_min"], entry["t_mid_residual_sigma"] = res, (res / sigma if sigma > 0 else None)
        timing_ok = abs(res) <= max(3.0 * sigma, TIMING_TOLERANCE_MIN)

        depth_ok = dur_ok = True
        if expected.get("depth_pct"):
            entry["depth_ratio"] = det["depth_pct"] / expected["depth_pct"]
            depth_ok = abs(det["depth_pct"] - expected["depth_pct"]) <= max(3.0 * det["depth_err_pct"], DEPTH_TOLERANCE * expected["depth_pct"])
        if expected.get("duration_h"):
            entry["duration_ratio"] = det["duration_h"] / expected["duration_h"]
            dur_ok = abs(det["duration_h"] - expected["duration_h"]) <= max(3.0 * det["duration_err_h"], DURATION_TOLERANCE * expected["duration_h"])

        entry["match"] = {"designation": expected["planet"], "source": "target ephemeris",
                          "timing_ok": timing_ok, "depth_ok": depth_ok, "duration_ok": dur_ok}
        if timing_ok and depth_ok and dur_ok:
            entry["classification"] = "known_transit_confirmed"
        elif not timing_ok:
            entry["classification"] = "known_transit_timing_offset"
            entry["notes"].append(
                f"Mid-time is {res:+.1f} min from the predicted time (tolerance {max(3.0 * sigma, TIMING_TOLERANCE_MIN):.0f} min). "
                "Catalogue ephemerides go stale (period errors, orbital decay, transit-timing variations); "
                "this is a measurement worth reporting, not an error in itself."
            )
        else:
            entry["classification"] = "known_transit_shape_mismatch"
            entry["notes"].append("Timing agrees, but the depth and/or duration differ from the catalogue by more than the tolerance.")
        return entry

    def _non_detection(self, findings: dict, expected: dict | None) -> dict[str, Any]:
        lc = findings.get("light_curve") or {}
        fit = findings.get("fit") or {}
        out: dict[str, Any] = {"expected": expected is not None}
        if not lc.get("t_s"):
            out["text"] = "No light curve was produced, so the expected transit could not be tested." if expected else "No light curve was produced."
            return out
        if expected is None:
            best = f" (best dip {fit['depth_pct']:.2f}% at SNR {fit['snr']:.1f}, not significant)" if fit.get("ok") else ""
            out["text"] = f"No transit detected, and none was expected in this window (control field){best}."
            return out

        t = np.asarray(lc["t_s"], dtype=float)
        t0 = _parse(lc["t0_iso"])
        tm = (_parse(expected["t_mid_iso"]) - t0).total_seconds()
        dur = (expected.get("duration_h") or (fit.get("duration_h") if fit.get("ok") else 2.0)) * 3600.0
        ingress = photometry.PhotometryParams().ingress_fraction * dur
        n_in = int((np.abs(t - tm) < dur / 2.0).sum())
        out["n_frames_in_expected_transit"] = n_in
        if n_in < 3:
            out["classification"] = "expected_window_not_covered"
            out["text"] = (
                f"The expected transit was not covered ({n_in} frame(s) inside the predicted {dur / 3600.0:.1f} h window); "
                "nothing can be said about it."
            )
            return out
        res = photometry.fit_fixed_transit(
            t, np.asarray(lc["flux"], dtype=float), np.asarray(lc["err"], dtype=float), tm, dur, ingress,
            order=int((findings.get("params") or {}).get("baseline_order", 1)),
        )
        depth, err = 100.0 * res["depth"], 100.0 * res["depth_err"]
        out.update(
            classification="expected_transit_not_detected",
            depth_at_ephemeris_pct=depth, depth_at_ephemeris_err_pct=err,
            depth_upper_limit_3sigma_pct=max(depth, 0.0) + 3.0 * err,
        )
        cat = expected.get("depth_pct")
        out["text"] = (
            f"No significant transit was detected. At the predicted time the depth is {depth:.2f} +/- {err:.2f} %"
            + (f" (catalogue {cat:.2f} %)" if cat else "")
            + f", a 3-sigma upper limit of {out['depth_upper_limit_3sigma_pct']:.2f} %."
            + (" This contradicts the catalogue depth: the ephemeris may be stale or the transit missed." if cat and out["depth_upper_limit_3sigma_pct"] < 0.7 * cat else "")
        )
        return out

    def _check_identity(self, findings: dict, catalogs: CatalogClient, target: dict) -> tuple[dict, dict]:
        """Calibrate the first frame against Gaia, then check the measured star is at the target position."""
        astro = findings.get("astrometry")
        if not astro:
            return {"status": "unavailable", "reason": "no WCS in frames"}, {"status": "unverified", "reason": "no WCS in frames"}
        wcs = wcs_from_dict(astro["wcs"])
        ny, nx = astro["frame_shape"]
        scale = pixel_scale_arcsec(wcs)
        radius = 0.5 * float(np.hypot(nx, ny)) * scale + 300.0
        try:
            rows = catalogs.gaia_cone(float(wcs.wcs.crval[0]), float(wcs.wcs.crval[1]), radius, GAIA_G_LIMIT,
                                     target=target, scene=self.mock_scene(target))
        except CatalogError as exc:
            log.warning("Gaia unavailable: %s", exc)
            return {"status": "unavailable", "reason": str(exc)}, {"status": "unverified", "reason": "Gaia unavailable"}
        if len(rows) < 8:
            reason = f"only {len(rows)} Gaia sources in field"
            return {"status": "failed", "reason": reason}, {"status": "unverified", "reason": reason}
        gaia_px = np.array(wcs.all_world2pix([r["ra_deg"] for r in rows], [r["dec_deg"] for r in rows], 0)).T
        inside = (gaia_px[:, 0] > -50) & (gaia_px[:, 0] < nx + 50) & (gaia_px[:, 1] > -50) & (gaia_px[:, 1] < ny + 50)
        est = estimate_translation(
            gaia_px[inside][:150], np.asarray(astro["reference_stars"]), max_shift=150.0, tol=2.0, min_matches=8
        )
        if est is None:
            reason = "no consistent star pattern match (the frames may be rotated relative to north)"
            return {"status": "failed", "reason": reason, "n_gaia": int(inside.sum())}, {"status": "unverified", "reason": reason}

        new_wcs = shift_wcs(wcs, est["dx"], est["dy"])
        calib = {
            "status": "calibrated", "dx_px": est["dx"], "dy_px": est["dy"],
            "offset_arcsec": float(np.hypot(est["dx"], est["dy"]) * scale),
            "n_matches": est["n_matches"], "rms_arcsec": est["rms_px"] * scale, "source": rows[0]["source"],
        }
        tx, ty = astro["target_xy"]
        ra, dec = new_wcs.all_pix2world([tx], [ty], 0)
        ra, dec = float(ra[0]) % 360.0, float(dec[0])
        sep = angular_separation_arcsec(ra, dec, float(target["ra_deg"]), float(target["dec_deg"]))
        nearest = min(rows, key=lambda r: angular_separation_arcsec(ra, dec, r["ra_deg"], r["dec_deg"]))
        identity = {
            "status": "confirmed" if sep <= IDENTITY_RADIUS_ARCSEC else "mismatch",
            "separation_arcsec": sep, "ra_deg": ra, "dec_deg": dec,
            "nearest_gaia_g_mag": nearest["g_mag"],
            "nearest_gaia_parallax_mas": nearest.get("parallax_mas"),
            "nearest_gaia_parallax_err_mas": nearest.get("parallax_err_mas"),
        }
        log.info(
            "Gaia calibration: offset %.1f arcsec from %d stars (rms %.2f arcsec); target star %.1f arcsec from the catalogue position",
            calib["offset_arcsec"], calib["n_matches"], calib["rms_arcsec"], sep,
        )
        return calib, identity

    # ------------------------------------------------------------------ #
    # 5. Upload files and anomalies (the default products; see core/submission.py, core/anomaly.py)
    # ------------------------------------------------------------------ #
    def _site(self) -> tuple[float, float, float]:
        """Latitude, longitude, elevation of the place the frames were taken."""
        s = self.settings
        return s.site_latitude_deg, s.site_longitude_deg, s.site_elevation_m

    def build_submissions(self, mission: dict) -> list[SubmissionDraft]:
        """One AAVSO Exoplanet Report per observed planet (the AAVSO Exoplanet Database feeds NASA Exoplanet Watch).

        A non-detection is submitted too: a well-measured light curve with no transit where one was
        expected is exactly the evidence an ephemeris refinement needs.
        """
        night = mission.get("night") or "unknown-night"
        return [self._aavso_draft(rec, night) for rec in mission.get("observations", [])]

    def _aavso_draft(self, rec: dict, night: str) -> SubmissionDraft:
        target, findings, xref = rec["target"], rec.get("findings") or {}, rec.get("crossref") or {}
        planet = target.get("planet") or target["name"]
        host = target.get("host") or planet.rsplit(" ", 1)[0]
        fname = f"{re.sub(r'[^A-Za-z0-9+.-]+', '', planet)}_{night}_AAVSO_Exoplanet.txt"
        draft = dict(
            network="AAVSO Exoplanet Database", kind="exoplanet_transit_photometry",
            format="AAVSO Exoplanet Report v0.62", filename=fname, target=planet,
        )
        if not target.get("planet"):  # a control field has no planet to report on
            return SubmissionDraft(**draft, content=None, blockers=["a control field: there is no exoplanet to report"])
        lc = findings.get("light_curve")
        if not lc or not lc.get("t_s"):
            return SubmissionDraft(**draft, content=None, blockers=[findings.get("summary") or "no light curve was produced"])

        blockers: list[str] = []
        notes: list[str] = []
        code = (self.settings.aavso_obs_code or "").strip()
        if not code:
            blockers.append("no AAVSO observer code is configured (set ASTRONOMO_AAVSO_OBS_CODE once the AAVSO has assigned one)")
        identity = xref.get("identity") or {}
        if identity.get("status") != "confirmed":
            blockers.append(
                f"the measured star was not confirmed to be the host (identity check: {identity.get('status', 'not run')}"
                + (f", {identity['reason']}" if identity.get("reason") else "") + ")"
            )
        for w in findings.get("warnings", []):
            notes.append(f"pipeline warning: {w}")

        lat, lon, elev = self._site()
        t0 = _parse(lc["t0_iso"])
        times = [t0 + timedelta(seconds=float(v)) for v in lc["t_s"]]  # the pipeline's times are UTC mid-exposure
        bjd = utc_to_bjd_tdb_many(times, target["ra_deg"], target["dec_deg"], lat, lon, elev)
        det = (findings.get("detections") or [None])[0]

        # The AAVSO's Rnflux is relative flux normalised to the out-of-transit level (nominally 1.0).  Use the
        # measured transit (padded by 20 %) to find that level; without a detection fall back to the series median.
        flux = [float(v) for v in lc["flux"]]
        err = [e for e in lc["err"]]
        baseline = statistics.median(flux)
        baseline_note = "the median of the whole series (no transit was measured, so the out-of-transit level is not known)"
        if det and det.get("epoch_mid_iso") and det.get("duration_h"):
            mid = _parse(det["epoch_mid_iso"])
            half = timedelta(hours=0.6 * float(det["duration_h"]))
            oot = [f for t, f in zip(times, flux) if abs(t - mid) > half]
            if len(oot) >= 5:
                baseline = statistics.median(oot)
                baseline_note = f"the median of {len(oot)} out-of-transit frames"
        if not math.isfinite(baseline) or baseline <= 0:
            return SubmissionDraft(**draft, content=None, blockers=["the flux baseline is not a positive number"])
        flux = [f / baseline for f in flux]
        err = [None if e is None else float(e) / baseline for e in err]
        rows = [(b, f, e) for b, f, e in zip(bjd, flux, err)]
        nd = xref.get("non_detection") or {}
        if det:
            tc = utc_to_bjd_tdb_many([_parse(det["epoch_mid_iso"])], target["ra_deg"], target["dec_deg"], lat, lon, elev)[0]
            result = (
                f"Transit detected. Tc={tc:.5f} BJD_TDB +/- {det['t_mid_err_min']:.1f} min; depth={det['depth_pct']:.2f} +/- "
                f"{det['depth_err_pct']:.2f} % (differential, instrumental); T14={det['duration_h']:.2f} h; "
                f"{det['coverage']} coverage. Trapezoid model, not limb-darkened."
            )
        else:
            result = f"No transit detected. {nd.get('text') or findings.get('summary') or ''}"
        exp = xref.get("expected") or {}
        priors = (
            f"Planning ephemeris: {target.get('ephemeris_ref') or 'catalogue'}; predicted Tc "
            f"{(exp.get('t_mid_iso') or '')[:16].replace('T', ' ')} UTC +/- {exp.get('t_mid_err_min', 0):.1f} min."
            if exp else None
        )
        scope = "Seestar S50 smart telescope (50 mm refractor, IMX462 CMOS camera, coded CCD per AAVSO guidance), IR-cut filter coded CV"
        notes_line = (
            f"{scope}. Differential photometry against {findings.get('n_comps', '?')} comparison stars, "
            f"{findings.get('n_frames_used', '?')} frames, rms {findings.get('rms_ppt', float('nan')):.1f} ppt. "
            f"Relative flux is normalised to {baseline_note}. "
            "Times are mid-exposure, converted from UTC to BJD_TDB."
        )
        try:
            content = upload_formats.aavso_exoplanet_report(
                obs_code=code or "XXXXX", software="Astronomo (transit_finder)", date_type="BJD_TDB",
                star_name=host, exoplanet_name=re.sub(r"\s+", "", planet), exposure_s=float(findings.get("exposure_s") or 10.0),
                filter_name="CV", measurement_type="Rnflux", rows=rows, ra_deg=target["ra_deg"], dec_deg=target["dec_deg"],
                priors=priors, results=result, notes=notes_line,
            )
        except upload_formats.FormatError as exc:
            return SubmissionDraft(**draft, content=None, blockers=[f"could not be written in the AAVSO format: {exc}"])

        figures = [f for f in findings.get("figures", []) if Path(f).is_file()]
        if not any(Path(f).name.startswith("finder") for f in figures):
            blockers.append(
                "no field image marking the target and comparison stars was written (the AAVSO form asks for one)"
            )
        notes += [
            "The header follows the published AAVSO Exoplanet report specification (v0.62); it has not been validated by the service itself.",
            "#DETREND_PARAMETERS is empty: the pipeline records no detrending columns, so the series is the raw differential flux.",
            "A field image of the first frame is attached, with the target (T) and the comparison stars (C) marked. "
            "These are the stars this light curve used, in pixel coordinates. The chart is not a new plate solution; "
            "the Gaia identity check is what says whether the measured star is the host.",
            "NASA Exoplanet Watch draws on the AAVSO Exoplanet Database; ExoClock has its own submission form and is not covered here.",
        ]
        return SubmissionDraft(**draft, content=content, blockers=blockers, notes=notes, attachments=figures)

    def detect_anomalies(self, mission: dict) -> list[dict]:
        """Timing offsets, missing transits and unexpected dips, each with a significance in sigma.

        * ``timing_offset``   the measured mid-time differs from the ephemeris by N sigma, using the quadrature sum of
          the fit's mid-time error and the ephemeris's own propagated error.  This is the TTV / stale-ephemeris signal.
        * ``missing_transit`` the expected transit is well covered but its depth at the predicted time is N sigma
          shallower than the catalogue depth.
        * ``unexpected_dip``  a significant dip where no transit is expected (a control field or an unknown companion).

        A target whose identity was not confirmed against Gaia is reported with significance 0 (the measured value is kept in
        the evidence): a timing offset measured on the wrong star is not an anomaly.  Shape (depth/duration) disagreement is
        not screened: it is dominated by aperture and blending systematics that the quoted errors do not include.
        """
        out: list[dict] = []
        for rec in mission.get("observations", []):
            target, findings, xref = rec["target"], rec.get("findings") or {}, rec.get("crossref") or {}
            name = target.get("planet") or target["name"]
            confirmed = (xref.get("identity") or {}).get("status") == "confirmed"
            det = (findings.get("detections") or [None])[0]
            entry = (xref.get("entries") or [{}])[0] if det else {}
            nd = xref.get("non_detection") or {}
            exp = xref.get("expected") or {}
            proposals: list[dict] = []

            sigma = entry.get("t_mid_residual_sigma")
            if det and sigma is not None and entry.get("t_mid_residual_min") is not None:
                res = entry["t_mid_residual_min"]
                ttv = " (a planet flagged for transit-timing variations)" if (target.get("network") or {}).get("task") == "ttv_monitoring" else ""
                proposals.append({
                    "kind": "timing_offset",
                    "summary": f"The {name} transit arrived {res:+.1f} min from its predicted time{ttv}.",
                    "significance_sigma": abs(sigma),
                    "evidence": {
                        "residual_min": res, "measured_t_mid_iso": det["epoch_mid_iso"], "measured_err_min": det.get("t_mid_err_min"),
                        "predicted_t_mid_iso": exp.get("t_mid_iso"), "predicted_err_min": exp.get("t_mid_err_min"),
                        "ephemeris": target.get("ephemeris_ref"),
                    },
                })
            if det and entry.get("classification") == "unexpected_dip" and det.get("depth_err_pct"):
                proposals.append({
                    "kind": "unexpected_dip",
                    "summary": f"A {det['depth_pct']:.2f} % dip of {det['duration_h']:.1f} h in {name}, where no transit was expected.",
                    "significance_sigma": det["depth_pct"] / det["depth_err_pct"],
                    "evidence": {"depth_pct": det["depth_pct"], "depth_err_pct": det["depth_err_pct"], "duration_h": det["duration_h"],
                                 "epoch_mid_iso": det["epoch_mid_iso"]},
                })
            cat = exp.get("depth_pct")
            if nd.get("classification") == "expected_transit_not_detected" and cat and nd.get("depth_at_ephemeris_err_pct"):
                gap = cat - nd["depth_at_ephemeris_pct"]
                proposals.append({
                    "kind": "missing_transit",
                    "summary": f"No transit of {name} at the predicted time: depth {nd['depth_at_ephemeris_pct']:.2f} +/- "
                               f"{nd['depth_at_ephemeris_err_pct']:.2f} % against {cat:.2f} % expected.",
                    "significance_sigma": gap / nd["depth_at_ephemeris_err_pct"],
                    "evidence": {"depth_at_ephemeris_pct": nd["depth_at_ephemeris_pct"], "catalogue_depth_pct": cat,
                                 "predicted_t_mid_iso": exp.get("t_mid_iso"), "n_frames_in_window": nd.get("n_frames_in_expected_transit")},
                })
            for prop in proposals:
                prop["target"] = name
                if not confirmed:
                    prop["evidence"]["measured_sigma"] = prop["significance_sigma"]
                    prop["evidence"]["identity"] = (xref.get("identity") or {}).get("status", "not run")
                    prop["summary"] += " (Not counted: the measured star was not confirmed to be the host.)"
                    prop["significance_sigma"] = 0.0
                out.append(prop)
        return out
