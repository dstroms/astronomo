"""Variable-star photometry for the AAVSO.

One visit measures one star against comparison stars in the same frames and reports a single
magnitude for the night (AAVSO Extended, green channel ``TG``).  An eclipsing binary whose
minimum falls inside the series also gets a time of minimum.  The AAVSO Target Tool does not
say when that minimum is, so a window saved from the Opportunities page is a usable night, and
the plan says so.

Simulation observes two fields: an eclipsing binary with a known minimum and magnitude, and a
steady control.  Both are recovered against those injected values.

Environment overrides (all optional)
    ASTRONOMO_VARSTAR_TARGETS       comma-separated AAVSO star names; replaces the ranked list
    ASTRONOMO_VARSTAR_LOOKAHEAD_H   how far ahead to look for a night (default 36)
    ASTRONOMO_VARSTAR_FRAMES        frames per star (default 40 live / 22 simulated)
    ASTRONOMO_VARSTAR_EXPOSURE_S    exposure per frame (default 10)
    ASTRONOMO_VARSTAR_INTERVAL_S    extra delay between frames (default 20 live / 480 simulated)
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from astropy.time import Time

from config import Settings
from core.base_plugin import BaseExperimentPlugin
from core.networks import TASK_MANUAL, NetworkError, network_block, normalize_name
from core.submission import SubmissionDraft
from plugins.variable_stars import networks, opportunities as opp, photometry
from plugins.variable_stars.scene import VariableScene
from tools.astrometry import (
    SEESTAR_S50_PIXEL_SCALE_ARCSEC,
    angular_separation_arcsec,
    estimate_translation,
    pixel_scale_arcsec,
    shift_wcs,
    wcs_from_dict,
)
from tools import upload_formats
from tools.catalogs import CatalogClient, CatalogError

log = logging.getLogger(__name__)

# Used only when the AAVSO list cannot be read.  Coordinates are J2000; magnitudes are the catalogue range.
FALLBACK_STARS = (
    {"name": "RR Lyr", "ra_deg": 291.3663, "dec_deg": 42.7844, "var_type": "RRab", "max_mag": 7.1, "min_mag": 8.1,
     "period_d": 0.5668, "sections": ["SPP"], "constellation": "Lyr"},
    {"name": "SS Cyg", "ra_deg": 325.6783, "dec_deg": 43.5861, "var_type": "UGSS", "max_mag": 8.2, "min_mag": 12.1,
     "period_d": None, "sections": ["CV"], "constellation": "Cyg"},
    {"name": "Z Cam", "ra_deg": 126.3049, "dec_deg": 73.1109, "var_type": "UGZ", "max_mag": 10.0, "min_mag": 14.5,
     "period_d": None, "sections": ["CV"], "constellation": "Cam"},
)
GAIA_G_LIMIT = 16.0
IDENTITY_RADIUS_ARCSEC = 12.0
COMP_MATCH_ARCSEC = 8.0
TIMING_TOLERANCE_MIN = 5.0  # a minimum this close to the prediction agrees, whatever the formal error
TIMING_FLOOR_MIN = 0.5  # clock and shutter, added in quadrature; not measured on this telescope
SIM_ECLIPSE_FRACTION = 0.34  # minimum duration as a fraction of the simulated span
SIM_DEPTH = 0.28  # fractional flux drop, about 0.36 mag, inside the fitter's range


def _env(name: str, default: str) -> str:
    return os.environ.get(f"ASTRONOMO_VARSTAR_{name}", default)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _parse(iso: str) -> datetime:
    when = datetime.fromisoformat(iso)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _window_note(sections: list[str] | None) -> str:
    if sections and "EB" in sections:
        return (
            "This window is a usable night, not an eclipse. The AAVSO list gives a period but not the time of "
            "minimum, so this plan is one brightness measurement, not a time of minimum."
        )
    return "This window is a usable night for one brightness measurement, not a timed event."


def _fallback_row(star: dict) -> dict[str, Any]:
    return {
        "name": star["name"], "vsx_oid": None, "ra_deg": star["ra_deg"], "dec_deg": star["dec_deg"],
        "constellation": star["constellation"], "var_type": star["var_type"],
        "max_mag": star["max_mag"], "max_band": "V", "min_mag": star["min_mag"], "min_band": "V",
        "period_d": star["period_d"], "cadence_d": 1.0, "sections": list(star["sections"]), "modes": ["V"],
        "last_observed_utc": None, "status": "never", "high_priority": False, "notes": "", "notices": [],
        "requests": [{"mode": "V", "cadence_d": 1.0, "last_observed_utc": None, "status": "never", "high_priority": False}],
    }


class VariableStarsPlugin(BaseExperimentPlugin):
    name = "variable_stars"
    description = (
        "Measures variable stars for the AAVSO: one differential magnitude per visit through the green channel, "
        "written as an AAVSO Extended file a person reviews."
    )

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

    def mock_scene(self, target: dict) -> VariableScene:
        return VariableScene.from_target(target)

    def opportunities(self, now: datetime | None = None, *, days: int = opp.HORIZON_DAYS, refresh: bool = False) -> dict:
        return opp.build_report(self.settings, now=now, days=days, refresh=refresh)

    def planned_target(self, opportunity: dict, window: dict) -> dict:
        return opp.planned_target(opportunity, window)

    def recheck_planned_target(self, target: dict) -> dict:
        return opp.recheck_planned(self.settings, target)

    def _simulated_targets(self) -> list[dict]:
        """An eclipsing binary with a known minimum, and a steady star as a control."""
        plan = self.get_exposure_plan()
        cadence = plan["exposure_s"] + plan["interval_s"]
        span = (plan["count"] - 1) * cadence + plan["exposure_s"]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        window = {"window_start": _iso(now), "window_end": _iso(now + timedelta(seconds=span)), "window_kind": "eclipse"}
        t_mid = span / 2.0
        duration = SIM_ECLIPSE_FRACTION * span
        systematics = {
            "drift_px_per_hr": [4.0, -2.5],
            "transparency_trend_per_hr": -0.02,
            "transparency_jitter": 0.004,
        }

        def star(perr: tuple[float, float], mag: float, depth: float) -> dict:
            scale = SEESTAR_S50_PIXEL_SCALE_ARCSEC
            return {
                "dx_px": perr[0] / scale, "dy_px": -perr[1] / scale, "mag": mag, "depth": depth,
                "t_mid_s": t_mid, "duration_s": duration, "ingress_s": 0.12 * duration,
            }

        perr_a, perr_b = (28.0, -17.0), (-35.0, 22.0)
        true_min = now + timedelta(seconds=t_mid)
        return [
            {
                "name": "V1234 Lac (sim)", "designation": "V1234 Lac", "ra_deg": 330.0, "dec_deg": 45.0,
                "priority": 2.0, **window, "v_mag": 11.3, "v_max_mag": 11.3, "v_min_mag": 11.7,
                "var_type": "EB", "period_d": 1.5, "sections": ["EB"],
                "predicted_min_iso": _iso(true_min), "predicted_min_err_min": 1.0,
                "sim": {
                    "seed": 41, "epoch0_iso": _iso(now), "pointing_error_arcsec": list(perr_a),
                    "eclipse": star(perr_a, 11.3, SIM_DEPTH), "mag": 11.3, **systematics,
                },
            },
            {
                "name": "Control star (sim)", "designation": "Control", "ra_deg": 200.0, "dec_deg": 35.0,
                "priority": 1.0, **window, "window_kind": "night", "v_mag": 11.8, "v_max_mag": 11.8, "v_min_mag": 11.8,
                "var_type": "constant", "sections": [],
                "sim": {
                    "seed": 7, "epoch0_iso": _iso(now), "pointing_error_arcsec": list(perr_b),
                    "eclipse": star(perr_b, 11.8, 0.0), "mag": 11.8, **systematics,
                },
            },
        ]

    def _live_targets(self) -> list[dict]:
        """Stars the AAVSO list says need data, that this telescope can reach on an upcoming night."""
        now = datetime.now(timezone.utc).replace(microsecond=0)
        limits = opp.Limits.from_env(self.settings)
        explicit = [n.strip() for n in _env("TARGETS", "").split(",") if n.strip()]
        source = "list"
        try:
            rows, info = networks.fetch_targets(
                self.settings.archive_cache_dir, self.settings.network_cache_ttl_h * 3600.0,
            )
        except NetworkError as exc:
            if explicit:
                raise CatalogError(f"the AAVSO Target Tool list could not be read ({exc})") from exc
            log.warning(
                "The AAVSO Target Tool list could not be read (%s). Using the built-in bright variables; "
                "this mission is NOT following the AAVSO's list.", exc,
            )
            rows, info, source = [_fallback_row(s) for s in FALLBACK_STARS], {
                "retrieved_utc": None, "stale_cache": False, "from_cache": False, "problems": [str(exc)],
            }, "fallback"
        rows = [opp.for_this_telescope(r) for r in rows if set(r.get("sections") or []) != {"EP"}]
        if explicit:
            wanted = {normalize_name(n) for n in explicit}
            rows = [r for r in rows if normalize_name(r["name"]) in wanted]
            if not rows:
                raise CatalogError(
                    "none of the named stars are on the AAVSO list in a filter this camera can serve: " + ", ".join(explicit)
                )
            source = "operator"
        return self._schedule(rows, info, now, limits, source)

    def _schedule(self, rows: list[dict], info: dict, now: datetime, limits: opp.Limits, source: str) -> list[dict]:
        site = (self.settings.site_latitude_deg, self.settings.site_longitude_deg, self.settings.site_elevation_m)
        open_rows = [r for r in rows if not opp.static_blockers(r, limits, site[0])]
        hours = float(_env("LOOKAHEAD_H", "36"))
        days = max(1, int(math.ceil(hours / 24.0)))
        slots = opp.night_windows(open_rows, now, days, limits, site)
        horizon = now + timedelta(hours=hours)
        ranked: list[tuple[float, datetime, dict, dict]] = []
        for row, slot in zip(open_rows, slots):
            windows = [w for w in slot["windows"] if w["start"] < horizon and w["end"] > now]
            if not windows:
                continue
            window = min(windows, key=lambda w: w["start"])
            if source == "list":
                net = opp.network_block_for(row, info, now, limits)
                score = float(net["score"] or 0.0)
            elif source == "operator":
                net = network_block(["operator"], TASK_MANUAL, "chosen by the operator (ASTRONOMO_VARSTAR_TARGETS)", score=1.0)
                score = 1.0
            else:
                net = network_block(
                    ["built_in"], TASK_MANUAL, "built-in bright variable; the AAVSO Target Tool list could not be read", score=1.0,
                )
                score = 1.0
            ranked.append((score, window["start"], row, {"window": window, "network": net}))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]["name"]))
        chosen = ranked[: self.target_limit()]
        if not chosen:
            raise CatalogError(
                f"no AAVSO star this telescope can measure has a night in the next {hours:g} h "
                f"(altitude, darkness, Moon, V {limits.saturate_vmag:g} to {limits.max_vmag:g})."
            )
        targets = []
        for score, start, row, extra in chosen:
            window = extra["window"]
            hours_to = max(0.0, (start - now).total_seconds() / 3600.0)
            targets.append({
                "name": row["name"], "designation": row["name"],
                "ra_deg": row["ra_deg"], "dec_deg": row["dec_deg"],
                "priority": 1000.0 - hours_to + score,
                "window_start": _iso(window["start"]), "window_end": _iso(window["end"]),
                "window_kind": "night", "window_note": _window_note(row.get("sections")),
                "v_mag": opp.brightest(row), "v_max_mag": row.get("max_mag"), "v_min_mag": row.get("min_mag"),
                "var_type": row.get("var_type"), "period_d": row.get("period_d"), "cadence_d": row.get("cadence_d"),
                "sections": list(row.get("sections") or []), "modes": list(row.get("modes") or []),
                "network": extra["network"],
            })
            log.info(
                "Variable %s: night %s to %s UTC. %s",
                row["name"], _iso(window["start"])[:16], _iso(window["end"])[11:16], extra["network"]["reason"],
            )
        return targets

    # ------------------------------------------------------------------ #
    # 2. Exposure plan
    # ------------------------------------------------------------------ #
    def get_exposure_plan(self) -> dict:
        """A short series live (one magnitude), a compressed night in simulation (so a minimum is sampled)."""
        live = not self.settings.offline_services
        return {
            "gain": 80,
            "exposure_s": float(_env("EXPOSURE_S", "10")),
            "filter": "IRCUT",
            "count": int(_env("FRAMES", "40" if live else "22")),
            "interval_s": float(_env("INTERVAL_S", "20" if live else "480")),
        }

    # ------------------------------------------------------------------ #
    # 3. Reduction
    # ------------------------------------------------------------------ #
    def analyze_frames(self, fits_paths: list[str]) -> dict:
        if not fits_paths:
            raise ValueError("analyze_frames() received no FITS files")
        return photometry.analyze(fits_paths, figure_dir=Path(fits_paths[0]).parent)

    def describe_hypothesis(self, targets: list[dict]) -> str:
        names = ", ".join(t["name"] for t in targets)
        return (
            "Many bright stars change brightness on timescales of hours to months, and the AAVSO asks amateurs to "
            f"keep measuring them. We observe {len(targets)} star(s) ({names}), measure each against comparison "
            "stars in the same frames, and report one green-channel magnitude for the night. A deep minimum in "
            "the series is timed as well. The result is a measurement for the record, not a discovery."
        )

    def llm_guidance(self) -> str:
        return (
            "Domain: variable-star differential photometry with a 50 mm colour camera. The reported magnitude is "
            "one point for the night on a Gaia G zero point through the green channel, not a transformed Johnson V "
            "magnitude. A fitted minimum is a dip in this series, not a confirmed eclipse of a known binary, unless "
            "the classification says it agrees with a prediction. An outburst flag is a lead. Do not call anything a discovery."
        )

    def summarize_for_llm(self, findings: dict, crossref: dict | None = None, target: dict | None = None) -> dict:
        xref = crossref or {}
        meas = xref.get("measurement") or {}
        eclipse = meas.get("eclipse") or {}
        out: dict[str, Any] = {
            "target": (target or {}).get("name"),
            "n_frames_used": findings.get("n_frames_used"),
            "n_comparison_stars": findings.get("n_comps"),
            "channel": findings.get("channel"),
            "classification": meas.get("classification"),
            "mag": None if meas.get("mag") is None else round(float(meas["mag"]), 3),
            "mag_err": None if meas.get("mag_err") is None else round(float(meas["mag_err"]), 3),
        }
        if eclipse.get("t_min_iso"):
            out["minimum_iso"] = eclipse["t_min_iso"]
            out["minimum_err_min"] = None if eclipse.get("t_min_err_min") is None else round(float(eclipse["t_min_err_min"]), 2)
        ident = xref.get("identity") or {}
        if ident.get("status"):
            out["identity"] = ident["status"]
        return {k: v for k, v in out.items() if v is not None}

    # ------------------------------------------------------------------ #
    # 4. Cross-reference: who the star is, and the magnitude
    # ------------------------------------------------------------------ #
    def cross_reference(self, findings: dict, catalogs: CatalogClient, target: dict) -> dict:
        calib, identity, gaia_rows = self._check_identity(findings, catalogs, target)
        result: dict[str, Any] = {
            "summary": "", "entries": [], "services": {"gaia_dr3": calib.get("source") or calib.get("status")},
            "calibration": calib, "identity": identity, "measurement": None, "notes": list(findings.get("warnings") or [])[:6],
        }
        if identity.get("status") == "mismatch":
            result["notes"].append(
                f"The measured star is {identity['separation_arcsec']:.0f} arcsec from the catalogue position: "
                "the wrong star may have been measured."
            )
        measurement = self._measurement(findings, target, calib, identity, gaia_rows)
        result["measurement"] = measurement
        if measurement and measurement.get("colour_term"):
            result["notes"].append(measurement["colour_note"])
        if measurement and measurement.get("trend_note"):
            result["notes"].append(measurement["trend_note"])
        if measurement and measurement.get("mag") is not None:
            eclipse = measurement.get("eclipse")
            result["summary"] = (
                f"{measurement['classification'].replace('_', ' ')}: "
                f"{measurement['mag']:.3f} +/- {measurement['mag_err']:.3f} mag"
                + (f", minimum at {eclipse['t_min_iso'][:16].replace('T', ' ')} UTC" if eclipse else "")
                + "."
            )
            result["entries"].append({"classification": measurement["classification"], "mag": measurement["mag"]})
        else:
            why = (measurement or {}).get("reason") or "no magnitude"
            result["summary"] = f"No magnitude for {target['name']} ({why})."
        return result

    def _measurement(self, findings: dict, target: dict, calib: dict, identity: dict, gaia_rows: list[dict]) -> dict[str, Any]:
        photo = findings.get("photometry") or {}
        curve = findings.get("light_curve") or {}
        target_flux = photo.get("target_flux") or []
        comp_flux = photo.get("comparison_flux") or []
        comp_xy = photo.get("comparison_xy") or []
        base: dict[str, Any] = {
            "mag": None, "mag_err": None, "classification": "uncalibrated", "eclipse": None,
            "channel": "TG" if findings.get("channel") == "green" else "TG",
            "channel_note": findings.get("channel_note") or "",
            "systematic_mag": photometry.SYSTEMATIC_MAG,
        }
        if calib.get("status") != "calibrated" or not target_flux or len(comp_flux) != len(target_flux):
            base["reason"] = calib.get("reason") or "no comparison-star fluxes"
            return base
        matched = self._match_comps(findings, calib, comp_xy, gaia_rows)
        if len(matched) < photometry.MIN_COMPS:
            base["reason"] = f"only {len(matched)} comparison star(s) matched a catalogue magnitude"
            base["zp_n"] = len(matched)
            return base
        order = [m["index"] for m in matched]
        fluxes = [[row[i] for i in order] for row in comp_flux]
        comp_mags = [m["mag"] for m in matched]
        mags = photometry.magnitudes_from_comps(target_flux, fluxes, comp_mags)
        if mags is None:
            base["reason"] = "the comparison fluxes could not be turned into a magnitude"
            return base
        plain_scatter = photometry.zp_scatter(fluxes, comp_mags)
        colour = self._colour_correction(findings, calib, gaia_rows, target_flux, fluxes, comp_mags, [m.get("bp_rp") for m in matched])
        if colour is not None:
            base["mag_without_colour_term"] = round(float(np.median(mags)), 4)
            base["colour_term"] = colour["term"]
            mags = colour["mags"]
        base["colour_note"] = (colour or {}).get("note") or self._colour_note_off
        base["zp_scatter_without_colour_term"] = None if not math.isfinite(plain_scatter[0]) else round(plain_scatter[0], 4)
        eclipse = self._eclipse(findings, curve)
        t_s = curve.get("t_s") or []
        point = photometry.night_point(
            mags, t_s if len(t_s) == len(mags) else None,
            t_mid_s=None if not eclipse else eclipse["t_mid_s"],
            duration_s=None if not eclipse else eclipse["duration_s"],
        )
        scatter, n_used = plain_scatter
        if colour is not None:  # what is left after the colour term, over the stars that fit the line
            scatter, n_used = colour["term"]["scatter"], colour["term"]["n"]
        # The comparison stars' own disagreement belongs in the quoted error when it exceeds the floor.
        if math.isfinite(scatter) and n_used >= 3:
            point["mag_err"] = float(math.hypot(point["mag_err"], scatter / math.sqrt(n_used)))
        base.update(
            mag=point["mag"], mag_err=point["mag_err"], n_frames=int(point["n"]),
            jd=self._jd(findings.get("epoch_mid_iso") or curve.get("t0_iso")),
            iso=findings.get("epoch_mid_iso") or curve.get("t0_iso"),
            zp_n=n_used, zp_scatter=None if not math.isfinite(scatter) else round(scatter, 4),
            calibration="gaia_g", eclipse=eclipse, series_mag=[round(float(v), 4) for v in mags],
        )
        trend = photometry.series_trend(mags)
        if trend is not None:
            base["trend_mag"] = round(trend, 4)
            if abs(trend) > max(0.05, 2.0 * base["mag_err"]):
                base["trend_note"] = (
                    f"The magnitude drifted {trend:+.2f} mag across the sequence, more than twice the quoted error "
                    f"({base['mag_err']:.2f}).  A real change in minutes is rare; check the per-frame watch for sky or "
                    "transparency changes.  Treat the error as larger than quoted."
                )
        base["classification"] = self._classify(base, target, identity)
        return base

    @staticmethod
    def _jd(iso: str | None) -> float | None:
        if not iso:
            return None
        return float(Time(_parse(iso)).jd)

    @staticmethod
    def _eclipse(findings: dict, curve: dict) -> dict[str, Any] | None:
        det = (findings.get("detections") or [None])[0]
        if not det or not det.get("epoch_mid_iso") or not curve.get("t0_iso"):
            return None
        t_mid_s = (_parse(det["epoch_mid_iso"]) - _parse(curve["t0_iso"])).total_seconds()
        depth = float(det["depth_pct"]) / 100.0
        depth_mag = None if not 0.0 < depth < 1.0 else float(-2.5 * math.log10(1.0 - depth))
        return {
            "t_min_iso": det["epoch_mid_iso"], "t_min_err_min": det.get("t_mid_err_min"),
            "t_mid_s": t_mid_s, "duration_s": float(det["duration_h"]) * 3600.0, "duration_h": det["duration_h"],
            "depth_pct": det["depth_pct"], "depth_mag": depth_mag, "snr": det.get("snr_median"),
        }

    def _classify(self, measurement: dict, target: dict, identity: dict) -> str:
        if identity.get("status") != "confirmed":
            return "identity_mismatch"
        if measurement.get("mag") is None:
            return "uncalibrated"
        eclipse = measurement.get("eclipse")
        predicted = target.get("predicted_min_iso")
        if eclipse and predicted:
            resid = (_parse(eclipse["t_min_iso"]) - _parse(predicted)).total_seconds() / 60.0
            tol = max(TIMING_TOLERANCE_MIN, 2.0 * self._timing_sigma_minutes(eclipse, target)[1])
            return "eclipse_on_time" if abs(resid) <= tol else "eclipse_offset"
        if eclipse:
            return "eclipse_unpredicted"
        bright = target.get("v_max_mag")
        if bright is None:
            bright = target.get("v_mag")
        if bright is not None and measurement["mag"] < float(bright) - max(3.0 * measurement["mag_err"], 0.2):
            return "brighter_than_range"
        return "steady"

    def _match_comps(self, findings: dict, calib: dict, comp_xy: list, rows: list[dict]) -> list[dict]:
        astro = findings.get("astrometry") or {}
        if not astro or not comp_xy or not rows or calib.get("status") != "calibrated":
            return []
        wcs = shift_wcs(wcs_from_dict(astro["wcs"]), calib["dx_px"], calib["dy_px"])
        tx, ty = astro["target_xy"]
        tra, tdec = wcs.all_pix2world([tx], [ty], 0)
        tra, tdec = float(tra[0]), float(tdec[0])
        matched = []
        for index, (x, y) in enumerate(comp_xy):
            ra, dec = wcs.all_pix2world([x], [y], 0)
            ra, dec = float(ra[0]), float(dec[0])
            if angular_separation_arcsec(ra, dec, tra, tdec) < COMP_MATCH_ARCSEC:
                continue  # this is the target, not a comparison
            nearest = min(rows, key=lambda r: angular_separation_arcsec(ra, dec, r["ra_deg"], r["dec_deg"]))
            sep = angular_separation_arcsec(ra, dec, nearest["ra_deg"], nearest["dec_deg"])
            if sep <= COMP_MATCH_ARCSEC and nearest.get("g_mag") is not None:
                matched.append({
                    "index": index, "mag": float(nearest["g_mag"]), "separation_arcsec": sep, "bp_rp": nearest.get("bp_rp"),
                })
        return matched

    _colour_note_off = "No colour term was applied (see `colour_term` when present)."

    def _colour_correction(
        self, findings: dict, calib: dict, gaia_rows: list[dict], target_flux: list, fluxes: list,
        comp_mags: list[float], comp_colours: list,
    ) -> dict[str, Any] | None:
        """Magnitudes with the comparison stars' colour term applied, or None (with the plain zero point used) when it cannot be.

        Needs the target's own Gaia colour and at least ``MIN_COLOUR_COMPS`` comparison stars spanning a range of
        colour.  Off with ``ASTRONOMO_VARSTAR_COLOUR_TERM=off``.
        """
        if os.environ.get("ASTRONOMO_VARSTAR_COLOUR_TERM", "on").strip().lower() in {"off", "0", "false", "no"}:
            return None
        target_colour = self._target_colour(findings, calib, gaia_rows)
        if target_colour is None:
            return None
        term = photometry.fit_colour_term(fluxes, comp_mags, comp_colours)
        if term is None:
            return None
        mags = photometry.magnitudes_with_colour_term(target_flux, fluxes, comp_mags, comp_colours, target_colour, term)
        if mags is None:
            return None
        term = {**term, "target_bp_rp": float(target_colour)}
        note = (
            f"Colour term applied: the zero point changes by {term['slope']:+.2f} mag per mag of Gaia BP-RP "
            f"(from {term['n']} comparison stars, {term['n_dropped']} set aside), evaluated at the target's colour "
            f"{target_colour:.2f}.  Scatter about the line {term['scatter']:.3f} mag, against {term['scatter_before']:.3f} without it."
        )
        return {"mags": mags, "term": term, "note": note}

    @staticmethod
    def _target_colour(findings: dict, calib: dict, rows: list[dict]) -> float | None:
        """Gaia BP-RP of the measured star (the catalogue source at its position), or None if it has none."""
        astro = findings.get("astrometry") or {}
        if not astro or not rows or calib.get("status") != "calibrated":
            return None
        wcs = shift_wcs(wcs_from_dict(astro["wcs"]), calib["dx_px"], calib["dy_px"])
        tx, ty = astro["target_xy"]
        ra, dec = wcs.all_pix2world([tx], [ty], 0)
        ra, dec = float(ra[0]), float(dec[0])
        nearest = min(rows, key=lambda r: angular_separation_arcsec(ra, dec, r["ra_deg"], r["dec_deg"]))
        if angular_separation_arcsec(ra, dec, nearest["ra_deg"], nearest["dec_deg"]) > COMP_MATCH_ARCSEC:
            return None
        return nearest.get("bp_rp")

    def _check_identity(self, findings: dict, catalogs: CatalogClient, target: dict) -> tuple[dict, dict, list]:
        """Calibrate the frame against Gaia and decide whether the measured star is the target.

        The Gaia rows are returned for the magnitude zero point.  They are not stored on the result.
        """
        astro = findings.get("astrometry")
        if not astro:
            # A frame can carry a WCS and still have no astrometry block: photometry returns before
            # writing one when it cannot choose stars.  The summary is that reason.
            summary = str(findings.get("summary") or "")
            reason = summary if findings.get("light_curve") is None and summary else "no WCS in the frames"
            return {"status": "unavailable", "reason": reason}, {"status": "unverified", "reason": reason}, []
        wcs = wcs_from_dict(astro["wcs"])
        ny, nx = astro["frame_shape"]
        scale = pixel_scale_arcsec(wcs)
        radius = 0.5 * float(np.hypot(nx, ny)) * scale + 300.0
        try:
            rows = catalogs.gaia_cone(
                float(wcs.wcs.crval[0]), float(wcs.wcs.crval[1]), radius, GAIA_G_LIMIT,
                target=target, scene=self.mock_scene(target),
            )
        except CatalogError as exc:
            log.warning("Gaia unavailable: %s", exc)
            return {"status": "unavailable", "reason": str(exc)}, {"status": "unverified", "reason": "Gaia unavailable"}, []
        if len(rows) < 8:
            reason = f"only {len(rows)} Gaia sources in the field"
            return {"status": "failed", "reason": reason}, {"status": "unverified", "reason": reason}, rows
        gaia_px = np.array(wcs.all_world2pix([r["ra_deg"] for r in rows], [r["dec_deg"] for r in rows], 0)).T
        inside = (gaia_px[:, 0] > -50) & (gaia_px[:, 0] < nx + 50) & (gaia_px[:, 1] > -50) & (gaia_px[:, 1] < ny + 50)
        est = estimate_translation(gaia_px[inside][:150], np.asarray(astro["reference_stars"]), max_shift=150.0, tol=2.0, min_matches=8)
        if est is None:
            reason = "no consistent star-pattern match"
            return {"status": "failed", "reason": reason, "n_gaia": int(inside.sum())}, {"status": "unverified", "reason": reason}, rows
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
        identity = {
            "status": "confirmed" if sep <= IDENTITY_RADIUS_ARCSEC else "mismatch",
            "separation_arcsec": sep, "ra_deg": ra, "dec_deg": dec,
        }
        log.info(
            "Gaia calibration: offset %.1f arcsec from %d stars; target star %.1f arcsec from the catalogue position",
            calib["offset_arcsec"], calib["n_matches"], sep,
        )
        return calib, identity, rows

    @staticmethod
    def _timing_sigma_minutes(eclipse: dict, target: dict) -> tuple[float, float]:
        """``(residual minutes, combined 1-sigma minutes)``.  Residual is measured minus predicted."""
        resid = (_parse(eclipse["t_min_iso"]) - _parse(target["predicted_min_iso"])).total_seconds() / 60.0
        parts = [TIMING_FLOOR_MIN]
        if eclipse.get("t_min_err_min"):
            parts.append(float(eclipse["t_min_err_min"]))
        if target.get("predicted_min_err_min"):
            parts.append(float(target["predicted_min_err_min"]))
        return resid, float(math.hypot(*parts))

    # ------------------------------------------------------------------ #
    # 5. Upload file and anomalies
    # ------------------------------------------------------------------ #
    def build_submissions(self, mission: dict) -> list[SubmissionDraft]:
        night = mission.get("night") or "unknown-night"
        return [self._aavso_draft(rec, night) for rec in mission.get("observations", [])]

    def _aavso_draft(self, rec: dict, night: str) -> SubmissionDraft:
        target = rec["target"]
        findings, xref = rec.get("findings") or {}, rec.get("crossref") or {}
        name = target["name"]
        safe = re.sub(r"[^A-Za-z0-9+.-]+", "", name) or "star"
        draft = dict(
            network="AAVSO (WebObs)", kind="variable_star_photometry", format="AAVSO Extended v1.2",
            target=name, filename=f"{safe}_{night}_AAVSO_Extended.txt",
        )
        meas = xref.get("measurement") or {}
        if meas.get("mag") is None or meas.get("jd") is None:
            return SubmissionDraft(**draft, content=None, blockers=[meas.get("reason") or findings.get("summary") or "no magnitude was measured"])
        blockers: list[str] = []
        code = (self.settings.aavso_obs_code or "").strip()
        if not code:
            blockers.append("no AAVSO observer code is configured (set ASTRONOMO_AAVSO_OBS_CODE once the AAVSO has assigned one)")
        identity = xref.get("identity") or {}
        if identity.get("status") != "confirmed":
            blockers.append(
                "the measured star was not confirmed to be the target "
                f"(identity check: {identity.get('status', 'not run')})"
            )
        eclipse = meas.get("eclipse") or {}
        notes_line = (
            f"One magnitude for the night, median of {meas.get('n_frames', '?')} frames"
            + (" outside the fitted minimum." if eclipse else ".")
            + f" Green channel (TG); {meas.get('channel_note') or 'channel not recorded'}. "
            f"Zero point from {meas.get('zp_n', '?')} Gaia DR3 G comparison stars"
            + (f" (scatter {meas['zp_scatter']:.3f} mag)." if meas.get("zp_scatter") is not None else ".")
            + f" A systematic floor of {photometry.SYSTEMATIC_MAG:.2f} mag is included in the error "
            "(no flat frames; TG is not transformed to Johnson V)."
        )
        if eclipse and eclipse.get("t_min_iso"):
            notes_line += (
                f" Fitted minimum {eclipse['t_min_iso'][:16].replace('T', ' ')} UTC"
                + (f" +/- {float(eclipse['t_min_err_min']):.1f} min." if eclipse.get("t_min_err_min") else ".")
            )
        try:
            content = upload_formats.aavso_extended_report(
                obs_code=code or "XXXXX", software="Astronomo (variable_stars)",
                rows=[{
                    "name": name, "jd": meas["jd"], "mag": meas["mag"], "merr": meas["mag_err"], "filt": "TG",
                    "trans": False, "mtype": "STD", "cname": "ENSEMBLE", "cmag": None, "chart": "Gaia DR3 G",
                    "notes": notes_line,
                }],
            )
        except upload_formats.FormatError as exc:
            return SubmissionDraft(**draft, content=None, blockers=[f"could not be written in the AAVSO format: {exc}"])
        figures = [f for f in findings.get("figures", []) if Path(f).is_file()]
        notes = [
            "The line is one brightness for the visit, not every frame. It follows AAVSO Extended v1.2 and has not been checked by the AAVSO's upload page.",
            "The magnitude is on a Gaia G zero point through the camera's green channel (TG), with no colour transformation. Say so if you send it.",
            "A person reviews this file and sends it. Astronomo does not.",
        ]
        if target.get("window_note"):
            notes.append(target["window_note"])
        return SubmissionDraft(**draft, content=content, blockers=blockers, notes=notes, attachments=figures)

    def detect_anomalies(self, mission: dict) -> list[dict]:
        """An eclipse far from its prediction, a star brighter than its catalogue maximum, or a Mira far from a predicted maximum.

        Each significance includes a systematic floor, so a small difference is not a large sigma.
        A star that was not confirmed against the catalogue is reported at 0 sigma: the number is kept, and it does not count.
        """
        out = []
        for rec in mission.get("observations", []):
            target, xref = rec["target"], rec.get("crossref") or {}
            meas = xref.get("measurement") or {}
            name = target["name"]
            confirmed = (xref.get("identity") or {}).get("status") == "confirmed"
            for prop in self._proposals(target, meas):
                prop["target"] = name
                if not confirmed:
                    prop["evidence"]["measured_sigma"] = prop["significance_sigma"]
                    prop["evidence"]["identity"] = (xref.get("identity") or {}).get("status", "not run")
                    prop["summary"] += " Not counted: the measured star was not confirmed."
                    prop["significance_sigma"] = 0.0
                out.append(prop)
        return out

    def _proposals(self, target: dict, meas: dict) -> list[dict]:
        props: list[dict] = []
        mag, err = meas.get("mag"), meas.get("mag_err")
        eclipse = meas.get("eclipse") or {}
        if eclipse.get("t_min_iso") and target.get("predicted_min_iso"):
            resid, sigma_min = self._timing_sigma_minutes(eclipse, target)
            props.append({
                "kind": "eclipse_timing",
                "summary": (
                    f"The minimum of {target['name']} fell {resid:+.1f} min from the predicted time "
                    f"(combined uncertainty {sigma_min:.1f} min)."
                ),
                "significance_sigma": abs(resid) / sigma_min if sigma_min else 0.0,
                "evidence": {
                    "residual_min": resid, "combined_err_min": sigma_min,
                    "t_min_iso": eclipse["t_min_iso"], "predicted_min_iso": target.get("predicted_min_iso"),
                },
            })
        bright = target.get("v_max_mag")
        if bright is None:
            bright = target.get("v_mag")
        if mag is not None and bright is not None and err:
            excess = float(bright) - float(mag)  # positive: brighter than the catalogued maximum
            floor = max(float(err), photometry.SYSTEMATIC_MAG)
            if excess > 0:
                props.append({
                    "kind": "outburst",
                    "summary": (
                        f"{target['name']} was measured at {mag:.2f} +/- {err:.2f}, {excess:.2f} mag brighter than "
                        f"its catalogued maximum of {float(bright):.2f}."
                    ),
                    "significance_sigma": excess / floor,
                    "evidence": {"mag": mag, "mag_err": err, "catalogue_max_mag": float(bright)},
                })
        predicted = target.get("predicted_mag")
        kind = str(target.get("var_type") or "")
        if mag is not None and predicted is not None and err and kind.upper().startswith("M"):
            faint = float(mag) - float(predicted)
            floor = max(float(err), 0.3)  # a phase prediction is rougher than one night's scatter
            if faint > 0:
                props.append({
                    "kind": "mira_offset",
                    "summary": (
                        f"{target['name']} was {mag:.2f} +/- {err:.2f}, {faint:.2f} mag fainter than the predicted "
                        f"maximum of {float(predicted):.2f}."
                    ),
                    "significance_sigma": faint / floor,
                    "evidence": {"mag": mag, "mag_err": err, "predicted_mag": float(predicted)},
                })
        return props
