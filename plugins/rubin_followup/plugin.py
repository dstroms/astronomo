"""Rubin follow-up plugin: observe public Rubin alerts with a small telescope and compare.

Science case: the Vera C. Rubin Observatory publishes alerts (positions and difference-image photometry
of things that changed) for everyone, through community brokers.  Rubin finds a transient; a 50 mm
Seestar can watch the bright ones evolve between Rubin visits.  This plugin

1. picks recent, bright, well-placed alerts from the public Fink/LSST API (or a watchlist of objects),
2. stacks a short sequence on each,
3. measures the source at the alert position (forced photometry, Gaia-calibrated), and
4. compares that with the object's public Rubin light curve: is it still there, brighter, fainter,
   or gone?

Everything specific to this science case lives in this package; Astronomo core only talks to the
:class:`~core.base_plugin.BaseExperimentPlugin` interface.

What this is *not*: Rubin images are not public (only alerts are), a 50 mm telescope reaches about
mag 15-16 in a few minutes while Rubin alerts are mostly fainter than 19, so few alerts are within
reach; and the comparison is between a broadband camera and a Rubin filter, good to a few tenths of a
magnitude at best.  docs/plugins/rubin.md spells this out.

Environment overrides (all optional)
    ASTRONOMO_RUBIN_FINK_URL         Fink API base URL (default https://api.lsst.fink-portal.org)
    ASTRONOMO_RUBIN_TAGS             comma list of Fink filter names (default extragalactic_lt20mag_candidate,in_tns)
    ASTRONOMO_RUBIN_OBJECT_IDS       comma list of Rubin diaObjectIds to follow regardless of brightness/age
    ASTRONOMO_RUBIN_MAX_MAG          faintest last-alert magnitude to attempt (default 16.0)
    ASTRONOMO_RUBIN_MAX_AGE_D        oldest last alert to attempt, in days (default 14)
    ASTRONOMO_RUBIN_MIN_RELIABILITY  lowest real/bogus score to attempt (default 0.8)
    ASTRONOMO_RUBIN_MIN_ALT_DEG      minimum altitude during the observation (default 30)
    ASTRONOMO_RUBIN_ALERTS_PER_TAG   alerts fetched per filter (default 500)
    ASTRONOMO_RUBIN_FRAMES           frames per target (default 20 simulated / 60 live)
    ASTRONOMO_RUBIN_EXPOSURE_S       exposure per frame (default 10)
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
from core.digest import round_floats, short
from core.networks import TASK_FOLLOWUP, network_block
from core.submission import SubmissionDraft
from plugins.rubin_followup import figures, fink, photometry
from plugins.rubin_followup.fink import FinkClient, FinkError
from plugins.rubin_followup.scene import TransientScene
from tools.astrometry import SEESTAR_S50_PIXEL_SCALE_ARCSEC, pixel_scale_arcsec, sun_altitude_deg, target_altitude_deg, wcs_from_dict
from tools import upload_formats
from tools.catalogs import CatalogClient, CatalogError
from tools.imaging import load_image

log = logging.getLogger(__name__)

DEFAULT_TAGS = "extragalactic_lt20mag_candidate,in_tns"
GAIA_G_LIMIT = 17.0
#: The Seestar is broadband (~G); these Rubin bands are the closest, in order of preference.
BAND_PREFERENCE = ("r", "g", "i", "z", "y", "u")
#: Floor on the agreement window.  Colour terms, the band mismatch and a source evolving between
#: the Rubin detection and ours are each a few tenths of a magnitude; we do not claim better.
MAG_TOLERANCE = 0.5
BAND_RECENCY_D = 3.0  # a band counts as "current" if it was detected within this of the newest detection
TREND_WINDOW_D = 10.0
MAX_TREND_MAG_PER_DAY = 1.5


def _env(name: str, default: str) -> str:
    return os.environ.get(f"ASTRONOMO_RUBIN_{name}", default)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(value: Any, spec: str = ".2f", missing: str = "--") -> str:
    return missing if value is None or (isinstance(value, float) and not math.isfinite(value)) else format(value, spec)


class RubinFollowupPlugin(BaseExperimentPlugin):
    name = "rubin_followup"
    description = "Follows up bright public Rubin Observatory alerts (via the Fink broker) and compares them with Rubin's light curve."
    status = "draft"
    status_note = "never run live, because the Rubin alert stream was dark, and never on a real telescope"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.simulated = settings.offline_services

    # ------------------------------------------------------------------ #
    # 1. Targets
    # ------------------------------------------------------------------ #
    def generate_targets(self) -> list[dict]:
        if self.settings.offline_services:
            return self._simulated_targets()
        return self._fink_targets()

    def _fink_client(self) -> FinkClient:
        return FinkClient()

    def _observable(self, ra: float, dec: float, start: datetime, end: datetime, min_alt: float) -> bool:
        s = self.settings
        for when in (start, end):
            if target_altitude_deg(ra, dec, s.site_latitude_deg, s.site_longitude_deg, s.site_elevation_m, when) < min_alt:
                return False
        if not s.mock_mode and sun_altitude_deg(s.site_latitude_deg, s.site_longitude_deg, s.site_elevation_m, start) > s.max_sun_altitude_deg:
            return False
        return True

    def _fink_targets(self) -> list[dict]:
        tags = [t.strip() for t in _env("TAGS", DEFAULT_TAGS).split(",") if t.strip()]
        watch_ids = [i.strip() for i in _env("OBJECT_IDS", "").split(",") if i.strip()]
        n_per_tag = int(_env("ALERTS_PER_TAG", "500"))
        max_mag = float(_env("MAX_MAG", "16.0"))
        max_age = float(_env("MAX_AGE_D", "14"))
        min_rel = float(_env("MIN_RELIABILITY", "0.8"))
        min_alt = float(_env("MIN_ALT_DEG", "30"))

        client = self._fink_client()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = now + timedelta(minutes=5)
        end = start + timedelta(hours=1)
        since = fink.cutoff(now, max_age)

        alerts: list[dict] = []
        failures: list[str] = []
        for tag in tags:
            try:
                alerts.extend(client.recent_alerts(tag, n=n_per_tag, since=since))
            except FinkError as exc:
                failures.append(f"{tag}: {exc}")
                log.warning("Fink filter %s failed: %s", tag, exc)
        history = {}
        if watch_ids:
            try:
                history = client.objects_history(watch_ids)
            except FinkError as exc:
                failures.append(f"watchlist: {exc}")
                log.warning("Fink watchlist query failed: %s", exc)
        if failures and len(failures) == len(tags) + (1 if watch_ids else 0):
            raise CatalogError("every Fink query failed: " + "; ".join(failures))

        if not alerts and not any(history.values()):
            newest = None
            try:
                newest = client.newest_alert_utc(tags[0]) if tags else None
            except FinkError:
                pass
            raise CatalogError(
                f"Fink has no Rubin alerts from the last {max_age:g} days for filter(s) {', '.join(tags) or '(none)'}; the newest it "
                f"holds is {newest.strftime('%Y-%m-%d') if newest else 'unknown'}. Rubin's alert stream has had long pauses; check "
                "https://ls.st/alert-stream-status, raise ASTRONOMO_RUBIN_MAX_AGE_D, or follow named objects with ASTRONOMO_RUBIN_OBJECT_IDS."
            )

        rejected = {"not a positive detection": 0, "low reliability": 0, "fainter than limit": 0, "too old": 0, "not observable": 0}
        candidates: list[tuple[dict, str]] = []
        unique_alerts = fink.latest_per_object(alerts)
        for a in unique_alerts:
            if a["mag"] is None or a["is_negative"]:
                rejected["not a positive detection"] += 1
            elif a["reliability"] is not None and a["reliability"] < min_rel:
                rejected["low reliability"] += 1
            elif a["mag"] > max_mag:
                rejected["fainter than limit"] += 1
            elif fink.days_between(a["utc_iso"], now) > max_age:
                rejected["too old"] += 1
            elif not self._observable(a["ra_deg"], a["dec_deg"], start, end, min_alt):
                rejected["not observable"] += 1
            else:
                candidates.append((a, "filter"))
        seen = {a["dia_object_id"] for a, _ in candidates}
        for oid, rows in history.items():
            last = next((r for r in reversed(rows) if r["mag"] is not None and not r["is_negative"]), None)
            if last is None or oid in seen:
                continue
            if self._observable(last["ra_deg"], last["dec_deg"], start, end, min_alt):
                candidates.append((last, "watchlist"))
            else:
                log.info("Watchlist object %s is below %.0f deg for the next hour; skipping", oid, min_alt)

        if not candidates:
            detail = ", ".join(f"{n} {why}" for why, n in rejected.items() if n)
            raise CatalogError(
                f"{len(unique_alerts)} Rubin alert(s) from the last {max_age:g} days, but none can be followed up now ({detail or 'no candidates'}). "
                f"Limits: mag <= {max_mag:g}, reliability >= {min_rel:g}, altitude >= {min_alt:g} deg. A 50 mm telescope reaches about "
                "mag 15-16, and most Rubin alerts are fainter than 19."
            )

        names: set[str] = set()
        targets = []
        for a, origin in candidates:
            name = a["tns_name"] or f"LSST {a['dia_object_id']}"
            if name in names:
                name = f"{name} ({a['dia_object_id']})"
            names.add(name)
            age = fink.days_between(a["utc_iso"], now)
            targets.append({
                "name": name, "designation": a["dia_object_id"], "ra_deg": a["ra_deg"], "dec_deg": a["dec_deg"],
                "priority": 100.0 - a["mag"] - 0.1 * age + (5.0 if origin == "watchlist" else 0.0),
                "window_start": _iso(start), "window_end": _iso(end),
                "network": network_block(
                    ["operator", "fink_lsst"] if origin == "watchlist" else ["fink_lsst"], TASK_FOLLOWUP,
                    f"Rubin alert published through the Fink broker: {a['band']}={a['mag']:.1f}, last seen {age:.1f} d ago"
                    + (f", reliability {a['reliability']:.2f}" if a["reliability"] is not None else "")
                    + (f", registered with the TNS as {a['tns_name']}" if a["tns_name"] else "")
                    + "; bright enough for a small telescope to measure between Rubin visits"
                    + ("; named on the operator's watchlist" if origin == "watchlist" else "") + ".",
                    score=100.0 - a["mag"] - 0.1 * age,
                    terms={"last_mag": a["mag"], "age_d": age},
                ),
                "rubin": {
                    "dia_object_id": a["dia_object_id"], "last_mag": round(a["mag"], 3),
                    "last_mag_err": None if a["mag_err"] is None else round(a["mag_err"], 3),
                    "last_band": a["band"], "last_utc_iso": a["utc_iso"], "age_d": round(age, 3),
                    "reliability": a["reliability"], "tns_name": a["tns_name"], "simbad_otype": a["simbad_otype"],
                    "origin": origin, "source": "fink-lsst",
                },
            })
            log.info("Rubin alert %s: %s=%.2f %.1f d ago, RA %.4f Dec %+.4f", name, a["band"], a["mag"], age, a["ra_deg"], a["dec_deg"])
        targets.sort(key=lambda t: -t["priority"])
        return targets

    # -- simulation ----------------------------------------------------------- #
    def _simulated_targets(self) -> list[dict]:
        """A fading supernova we still see, an alert that has vanished, and a source rising faster than predicted."""
        now = datetime.now(timezone.utc).replace(microsecond=0)
        window = {"window_start": _iso(now), "window_end": _iso(now + timedelta(hours=2))}
        scale = SEESTAR_S50_PIXEL_SCALE_ARCSEC
        systematics = {"drift_px_per_hr": [60.0, -40.0], "transparency_jitter": 0.01}

        def points(spec: list[tuple[float, str, float]]) -> list[dict]:
            pts = []
            for days_ago, band, mag in spec:
                when = now - timedelta(days=days_ago)
                pts.append({"utc_iso": _iso(when), "mjd": float(Time(when).tai.mjd), "band": band, "mag": mag, "mag_err": 0.04})
            return sorted(pts, key=lambda p: p["mjd"])

        def build(name: str, tns: str, oid: str, ra: float, dec: float, seed: int, perr: tuple[float, float],
                  history: list[dict], mag_now: float | None) -> dict:
            last = history[-1]
            return {
                "name": name, "designation": oid, "ra_deg": ra, "dec_deg": dec,
                "priority": 100.0 - last["mag"], **window,
                "rubin": {
                    "dia_object_id": oid, "last_mag": last["mag"], "last_mag_err": last["mag_err"], "last_band": last["band"],
                    "last_utc_iso": last["utc_iso"], "age_d": fink.days_between(last["utc_iso"], now), "reliability": 0.99,
                    "tns_name": tns, "simbad_otype": None, "origin": "filter", "source": "simulated", "history": history,
                },
                # The transient sits at its Rubin position; the mount lands perr arcsec away, so on the
                # detector it is offset from the centre by the opposite amount (north up, east left).
                "sim": {"seed": seed, "epoch0_iso": _iso(now), "pointing_error_arcsec": list(perr),
                        "transient": {"dx_px": perr[0] / scale, "dy_px": -perr[1] / scale, "mag": mag_now, "in_gaia": False},
                        **systematics},
            }

        fading = points([(9.1, "r", 15.40), (7.0, "r", 15.72), (5.1, "r", 16.01), (3.2, "r", 16.30), (5.0, "g", 16.20), (3.1, "g", 16.55)])
        gone = points([(2.4, "r", 15.60), (2.0, "r", 15.80)])
        rising = points([(1.6, "r", 17.95), (1.2, "r", 17.62), (0.8, "r", 17.28)])
        return [
            build("AT 2026sna (sim)", "AT 2026sna", "900000000000000001", 52.30, -27.80, 41, (28.0, -17.0), fading, 16.80),
            build("AT 2026snb (sim)", "AT 2026snb", "900000000000000002", 150.10, 2.20, 42, (-35.0, 22.0), gone, None),
            build("AT 2026snc (sim)", "AT 2026snc", "900000000000000003", 62.00, -48.70, 43, (12.0, 31.0), rising, 15.70),
        ]

    def mock_scene(self, target: dict) -> TransientScene:
        return TransientScene.from_target(target)

    # ------------------------------------------------------------------ #
    # 2. Exposure plan
    # ------------------------------------------------------------------ #
    def get_exposure_plan(self) -> dict:
        live = not self.settings.offline_services
        return {
            "gain": 80,
            "exposure_s": float(_env("EXPOSURE_S", "10")),
            "filter": "IRCUT",
            "count": int(_env("FRAMES", "60" if live else "20")),
            "interval_s": 0.0,  # the target is static: stack back to back
        }

    # ------------------------------------------------------------------ #
    # 3. Reduction
    # ------------------------------------------------------------------ #
    def analyze_frames(self, fits_paths: list[str]) -> dict:
        if not fits_paths:
            raise ValueError("analyze_frames() received no FITS files")
        findings = photometry.analyze(fits_paths, figure_dir=Path(fits_paths[0]).parent)
        findings["plugin"] = self.name
        return findings

    # ------------------------------------------------------------------ #
    # 4. Cross-reference
    # ------------------------------------------------------------------ #
    def describe_hypothesis(self, targets: list[dict]) -> str:
        names = ", ".join(t["name"] for t in targets)
        return (
            "Rubin Observatory publishes alerts for sources that changed. A bright alert should still be visible to a small "
            f"telescope hours to days later. We stack {len(targets)} alert position(s) ({names}) and test whether the source is "
            "detected, how bright it is, and whether that agrees with where Rubin's own light curve says it should be."
        )

    def llm_guidance(self) -> str:
        return (
            "Domain: follow-up of public Rubin Observatory alerts with a 50 mm broadband camera. Magnitudes are Gaia-G-like, "
            "Rubin's are AB in one band, so differences below about 0.5 mag are not meaningful. Rubin alert photometry is "
            "difference-image flux. 'not_detected_expected_visible' means we should have seen it if it behaved as Rubin's light "
            "curve implies; it can mean a fast fader, a moving object, an artefact or a position error. It is never a discovery. "
            "Do not describe an upper limit as a measurement."
        )

    def summarize_for_llm(self, findings: dict, crossref: dict | None = None, target: dict | None = None) -> dict:
        """Concise JSON: stack summary, Rubin expectation, our measurement.  No sources, no light curve, no pixels."""
        base = findings.get("agent_summary") or photometry.summarize(findings)
        out: dict[str, Any] = {"target": (target or {}).get("name"), **base}
        xref = crossref or {}
        rub = xref.get("rubin") or {}
        if rub:
            out["rubin"] = {k: rub.get(k) for k in ("band", "last_mag", "age_d", "trend_mag_per_day", "expected_mag", "basis") if rub.get(k) is not None}
        for entry in (xref.get("entries") or [])[:1]:
            out["result"] = {
                k: (round(entry[k], 2) if isinstance(entry.get(k), float) else entry.get(k))
                for k in ("classification", "mag", "mag_err", "limit_mag_5sigma", "snr", "delta_mag") if entry.get(k) is not None
            }
        cal = xref.get("calibration") or {}
        if cal.get("status"):
            out["calibration"] = {"status": cal["status"], **{k: cal[k] for k in ("zp_n", "zp_scatter") if cal.get(k) is not None}}
        notes = [short(n, 140) for n in (xref.get("notes") or [])[:2]]
        if notes:
            out["notes"] = notes
        return round_floats(out, 4)

    # -- Rubin side ----------------------------------------------------------- #
    def _history(self, target: dict, notes: list[str]) -> list[dict]:
        """The object's Rubin light curve (simulated, or fetched live), falling back to the alert that selected it."""
        rubin = target.get("rubin") or {}
        if rubin.get("history"):
            return list(rubin["history"])
        pts: list[dict] = []
        oid = rubin.get("dia_object_id")
        if oid and not self.settings.offline_services:
            try:
                pts = fink.light_curve_points(self._fink_client().object_history(str(oid)))
            except FinkError as exc:
                notes.append(f"Rubin light curve unavailable ({short(exc, 120)}); using the selecting alert only.")
        if not pts and rubin.get("last_mag") is not None and rubin.get("last_utc_iso"):
            when = _parse(rubin["last_utc_iso"])
            pts = [{"utc_iso": rubin["last_utc_iso"], "mjd": float(Time(when).tai.mjd), "band": rubin.get("last_band") or "?",
                    "mag": float(rubin["last_mag"]), "mag_err": rubin.get("last_mag_err")}]
        return pts

    @staticmethod
    def expected_from_history(points: list[dict], when: datetime) -> dict | None:
        """What Rubin's own photometry predicts for ``when``: the last point, or a short linear trend."""
        if not points:
            return None
        # Prefer the band closest to the camera's, but only among bands with a *recent* detection: a stale
        # r-band point must not outrank a fresh g-band one.
        newest = max(p["mjd"] for p in points)
        fresh = {p["band"] for p in points if newest - p["mjd"] <= BAND_RECENCY_D}
        band = next((b for b in BAND_PREFERENCE if b in fresh), points[-1]["band"])
        pts = [p for p in points if p["band"] == band]
        last = pts[-1]
        age = fink.days_between(last["utc_iso"], when)
        out: dict[str, Any] = {
            "band": band, "last_mag": last["mag"], "last_mag_err": last.get("mag_err"), "last_utc_iso": last["utc_iso"],
            "age_d": age, "n_points_band": len(pts), "trend_mag_per_day": None, "expected_mag": last["mag"],
            "expected_err": float(last.get("mag_err") or 0.1), "basis": "last Rubin detection",
        }
        recent = [p for p in pts if last["mjd"] - p["mjd"] <= TREND_WINDOW_D][-6:]
        if len(recent) >= 3:
            t = np.array([p["mjd"] for p in recent]) - recent[-1]["mjd"]
            m = np.array([p["mag"] for p in recent])
            baseline = float(t[-1] - t[0])
            if baseline >= 0.5:
                slope, icpt = (float(v) for v in np.polyfit(t, m, 1))
                if abs(slope) <= MAX_TREND_MAG_PER_DAY and 0.0 <= age <= 2.0 * baseline:
                    expected = icpt + slope * age
                    out.update(
                        trend_mag_per_day=slope, expected_mag=float(expected),
                        expected_err=float(math.hypot(out["expected_err"], 0.25 * abs(expected - last["mag"]))),
                        basis=f"linear trend of {len(recent)} {band}-band points over {baseline:.1f} d",
                    )
        return out

    # -- orchestration -------------------------------------------------------- #
    def cross_reference(self, findings: dict, catalogs: CatalogClient, target: dict) -> dict:
        notes: list[str] = []
        result: dict[str, Any] = {
            "summary": "", "entries": [], "services": {}, "calibration": None, "rubin": None, "measurement": None,
            "light_curve": None, "gaia_counterpart": None, "notes": notes, "figures": [],
        }
        rubin_src = (target.get("rubin") or {}).get("source") or "fink-lsst"
        result["services"]["rubin_alerts"] = "simulated" if self.settings.offline_services else f"{rubin_src} (public REST API)"

        when = _parse(findings["epoch_mid_iso"]) if findings.get("epoch_mid_iso") else datetime.now(timezone.utc)
        points = self._history(target, notes)
        expected = self.expected_from_history(points, when)
        result["rubin"] = expected

        stack_info = findings.get("stack") or {}
        if not stack_info.get("path") or not Path(stack_info["path"]).is_file() or not findings.get("astrometry"):
            reason = "no stack could be built" if not stack_info.get("path") else "no usable stack file or pointing information"
            result["services"]["gaia_dr3"] = "not queried"
            result["entries"].append(self._entry("not_measured", expected, notes=[f"The target could not be measured: {reason}."]))
            result["summary"] = f"Not measured: {reason}. " + (findings.get("summary") or "")
            return result

        calib, gaia_rows = self._calibrate(findings, catalogs, target)
        result["calibration"] = {k: v for k, v in calib.items() if k != "wcs"}
        result["services"]["gaia_dr3"] = (gaia_rows[0].get("source") if gaia_rows else None) or calib["status"]
        if calib["status"] not in ("calibrated", "astrometry_only"):
            result["entries"].append(self._entry("not_measured", expected, notes=[f"Astrometric calibration failed: {calib.get('reason')}."]))
            result["summary"] = f"Not measured: astrometric calibration failed ({calib.get('reason')})."
            return result

        wcs = wcs_from_dict(calib["wcs"])
        ra, dec = float(target["ra_deg"]), float(target["dec_deg"])
        xy = wcs.all_world2pix([ra], [dec], 0)
        x, y = float(xy[0][0]), float(xy[1][0])
        data, _ = load_image(stack_info["path"])
        params = photometry.PhotometryParams()
        meas = photometry.measure_target(
            data, float(stack_info["rms_adu"]), int(stack_info["n_frames"]), (x, y), findings.get("sources") or [],
            calib.get("zero_point"), calib.get("zp_err") or 0.0, params, seed=int(target.get("db_id") or 0),
        )
        result["measurement"] = meas

        cp = photometry.gaia_counterpart(gaia_rows, ra, dec)
        result["gaia_counterpart"] = cp
        if cp:
            notes.append(
                f"A Gaia DR3 star (G={cp['g_mag']:.1f}) lies {cp['separation_arcsec']:.1f} arcsec from the alert position: this may be "
                "a known variable star or a blend, not a new transient. Rubin's alert flux is a difference flux, so it can differ "
                "from this star's total brightness."
            )
        if meas.get("brighter_neighbour_px") is not None and meas.get("detected"):
            notes.append(f"A brighter source lies {meas['brighter_neighbour_px']:.1f} px from the measurement position; the flux may be contaminated.")
        if expected and expected["age_d"] > 7.0:
            notes.append(f"The last Rubin detection is {expected['age_d']:.0f} d old; the source may have changed a lot since.")
        if calib["status"] == "astrometry_only":
            notes.append(f"No photometric zero point ({calib.get('reason')}); only the instrumental signal-to-noise is available.")
        if meas.get("status") == "ok" and meas.get("empirical_noise_adu") and meas["empirical_noise_adu"] > 1.5 * meas["analytic_noise_adu"]:
            notes.append("The measured sky scatter near the target is well above the photon-noise estimate (a bright host or crowding); "
                         "the error uses the measured scatter.")

        klass, delta, tol = self._classify(meas, calib, expected)
        entry = self._entry(klass, expected, meas, delta, tol, notes=[])
        result["entries"].append(entry)
        result["summary"] = self._summary(target, klass, meas, expected, delta)
        result["light_curve"] = {
            "rubin": points[-200:],
            "ours": {"utc_iso": _iso(when), "mag": meas.get("mag"), "mag_err": meas.get("mag_err"),
                     "limit_mag_5sigma": meas.get("limit_mag_5sigma"), "detected": bool(meas.get("detected"))},
        }
        try:
            result["figures"].append(figures.follow_up_figure(
                Path(stack_info["path"]).parent / "follow_up.png", data, (x, y), target["name"], result["light_curve"], expected,
                klass, params,
            ))
        except Exception:  # noqa: BLE001 - a plotting bug must not discard the science
            log.exception("Rubin follow-up: figure generation failed")
            notes.append("Figure generation failed; see logs.")
        return result

    def _calibrate(self, findings: dict, catalogs: CatalogClient, target: dict) -> tuple[dict, list[dict]]:
        astro = findings["astrometry"]
        wcs = wcs_from_dict(astro["wcs"])
        ny, nx = astro["frame_shape"]
        radius = 0.5 * float(np.hypot(nx, ny)) * pixel_scale_arcsec(wcs) + 300.0
        try:
            rows = catalogs.gaia_cone(float(wcs.wcs.crval[0]), float(wcs.wcs.crval[1]), radius, GAIA_G_LIMIT,
                                      target=target, scene=self.mock_scene(target))
        except CatalogError as exc:
            log.warning("Gaia unavailable: %s", exc)
            return {"status": "failed", "reason": f"Gaia unavailable: {exc}"}, []
        calib = photometry.calibrate(astro, findings.get("sources") or [], rows)
        if calib["status"] == "calibrated":
            log.info(
                "Gaia calibration: offset %.1f arcsec (%d stars, rms %.2f arcsec); zero point %.3f +/- %.3f from %d stars (scatter %.3f)",
                calib["offset_arcsec"], calib["n_matches"], calib["rms_arcsec"], calib["zero_point"], calib["zp_err"], calib["zp_n"], calib["zp_scatter"],
            )
        return calib, rows

    # -- classification ------------------------------------------------------- #
    @staticmethod
    def _classify(meas: dict, calib: dict, expected: dict | None) -> tuple[str, float | None, float | None]:
        """``(classification, measured - expected in mag, tolerance in mag)``."""
        status = meas.get("status")
        if status != "ok":
            return ("not_covered", None, None)
        detected, marginal = bool(meas.get("detected")), bool(meas.get("marginal"))
        if calib.get("zero_point") is None:
            return ("detected_uncalibrated" if detected else "not_detected_uncalibrated", None, None)
        if detected or marginal:
            if expected is None:
                return ("detected_no_rubin_reference" if detected else "marginal_detection", None, None)
            delta = float(meas["mag"] - expected["expected_mag"])
            sigma = math.hypot(float(meas.get("mag_err") or 0.0), float(expected["expected_err"]))
            tol = max(MAG_TOLERANCE, 3.0 * sigma)
            if marginal:
                return ("marginal_detection", delta, tol)
            if abs(delta) <= tol:
                return ("detected_consistent_with_rubin", delta, tol)
            return ("detected_brighter_than_expected" if delta < 0 else "detected_fainter_than_expected", delta, tol)
        limit = meas.get("limit_mag_5sigma")
        if expected is None or limit is None:
            return ("not_detected", None, None)
        delta = float(limit - expected["expected_mag"])  # > 0: we reach deeper than the source should be
        if delta > MAG_TOLERANCE:
            return ("not_detected_expected_visible", delta, MAG_TOLERANCE)
        return ("not_detected_uninformative", delta, MAG_TOLERANCE)

    @staticmethod
    def _entry(klass: str, expected: dict | None, meas: dict | None = None, delta: float | None = None, tol: float | None = None,
               notes: list[str] | None = None) -> dict[str, Any]:
        meas = meas or {}
        entry: dict[str, Any] = {
            "detection_id": "FU01", "classification": klass, "notes": list(notes or []),
            "mag": meas.get("mag"), "mag_err": meas.get("mag_err"), "limit_mag_5sigma": meas.get("limit_mag_5sigma"),
            "snr": meas.get("snr"), "delta_mag": delta, "tolerance_mag": tol,
            "expected_mag": (expected or {}).get("expected_mag"),
        }
        return entry

    @staticmethod
    def _summary(target: dict, klass: str, meas: dict, exp: dict | None, delta: float | None) -> str:
        f = _fmt
        ref = ""
        if exp:
            how = f"the {exp['band']}-band trend predicts {f(exp['expected_mag'])}" if exp.get("trend_mag_per_day") is not None else \
                  f"its last Rubin {exp['band']}-band detection was {f(exp['last_mag'])}"
            ref = f"{exp['age_d']:.1f} d after Rubin's last detection, {how}"
        got = (f"G~{f(meas.get('mag'))} +/- {f(meas.get('mag_err'))} (SNR {f(meas.get('snr'), '.0f')})" if meas.get("mag") is not None
               else f"SNR {f(meas.get('snr'), '.1f')}")
        lim = f(meas.get("limit_mag_5sigma"), ".1f")
        text = {
            "detected_consistent_with_rubin": f"Detected at {got}; {ref}: consistent (delta {f(delta, '+.2f')} mag).",
            "detected_brighter_than_expected": f"Detected at {got}; {ref}: brighter by {f(abs(delta) if delta is not None else None)} mag (still rising, re-brightening, or a colour effect).",
            "detected_fainter_than_expected": f"Detected at {got}; {ref}: fainter by {f(delta)} mag (fading faster than predicted, or a colour effect).",
            "detected_no_rubin_reference": f"Detected at {got}; no Rubin photometry was available to compare with.",
            "marginal_detection": f"Marginal signal at {got}; below the 5-sigma detection threshold (limit G~{lim}).",
            "not_detected_expected_visible": f"Not detected (5-sigma limit G~{lim}); {ref}, so it should have been visible at least {f(delta)} mag above the limit: faded faster than predicted, moved, was not real, or the position is off.",
            "not_detected_uninformative": f"Not detected (5-sigma limit G~{lim}); {ref}, which is too faint for this stack to say anything.",
            "not_detected": f"Not detected (5-sigma limit G~{lim}); no Rubin photometry to compare with.",
            "detected_uncalibrated": f"Detected at SNR {f(meas.get('snr'), '.1f')} but no photometric zero point was available.",
            "not_detected_uncalibrated": f"Not detected (SNR {f(meas.get('snr'), '.1f')}); no photometric zero point was available.",
            "not_covered": f"The alert position is not usable in this stack ({meas.get('status')}).",
        }
        return f"{target['name']}: " + text.get(klass, klass)

    # ------------------------------------------------------------------ #
    # 5. Upload files and anomalies (the default products; see core/submission.py, core/anomaly.py)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _jd(iso: str) -> float:
        return float(Time(_parse(iso)).jd)  # UTC Julian date, as the AAVSO Extended format asks

    def result_notes(self, observation: dict) -> dict:
        """The magnitude (or the limit it was not seen at), its error and how it compares with Rubin's own light curve."""
        xref = observation.get("crossref") or {}
        meas = xref.get("measurement") or {}
        limit = meas.get("limit_mag_5sigma")
        if meas.get("mag") is not None:
            measured = f"G ~ {float(meas['mag']):.2f} mag on the Gaia G scale"
            if meas.get("snr") is not None:
                measured += f" (SNR {float(meas['snr']):.0f})"
            error = (
                f"+/- {float(meas['mag_err']):.2f} mag: the stack's statistical error and the zero-point scatter. "
                "No colour term is applied, so a colour-dependent systematic of a few tenths of a magnitude is not in it."
            ) if meas.get("mag_err") is not None else "No error was quoted for this magnitude."
        elif limit is not None:
            measured = f"Not detected. The 5-sigma limit is G ~ {float(limit):.1f} mag. This is an upper limit, not a measurement."
            error = "None: an upper limit has no error."
        else:
            measured = "No measurement was made."
            error = "None: nothing was measured."
        return {"measured": measured, "error": error, "compared": xref.get("summary")}

    def caveats(self) -> list[str]:
        return [
            "This plugin is a draft and has not run live: the alert stream was dark when it was written.",
            "Most Rubin alerts are fainter than a 50 mm telescope reaches. A signal near the limit is marginal, and an "
            "upper limit must never be described as a measurement.",
            "The magnitude is on the Gaia G scale through a zero point, with no colour term, so it carries a "
            "colour-dependent systematic of a few tenths of a magnitude. It is not transformed to another band.",
            "The screen only looks for an outburst. It does not look for fading or a disappearance, and an outburst flag "
            "is a lead for a person to check, not a discovery.",
        ]

    def build_submissions(self, mission: dict) -> list[SubmissionDraft]:
        """One AAVSO Extended report per observed alert: a magnitude if the source was measured, else a fainter-than limit.

        The brightness is on the Gaia G scale with no colour term and an IR-cut clear filter, so it is coded CV with
        ``TRANS=NO`` and the Gaia zero point is stated in the notes: it is not a V-band standard magnitude, and the
        notes say so.  Objects the AAVSO does not know cannot be loaded, which is a person's check to make.
        """
        night = mission.get("night") or "unknown-night"
        return [self._aavso_draft(rec, night) for rec in mission.get("observations", [])]

    def _aavso_draft(self, rec: dict, night: str) -> SubmissionDraft:
        target, findings, xref = rec["target"], rec.get("findings") or {}, rec.get("crossref") or {}
        name = target["name"]
        draft = dict(
            network="AAVSO (WebObs)", kind="transient_photometry", format="AAVSO Extended v1.2", target=name,
            filename=f"{re.sub(r'[^A-Za-z0-9+.-]+', '', name)}_{night}_AAVSO_Extended.txt",
        )
        entry = (xref.get("entries") or [{}])[0]
        calib = xref.get("calibration") or {}
        klass = entry.get("classification", "not_measured")
        if klass.startswith("detected") and entry.get("mag") is not None:
            mag: Any = entry["mag"]
            merr = entry.get("mag_err")
        elif entry.get("limit_mag_5sigma") is not None and klass.startswith(("not_detected", "marginal")):
            mag, merr = ("<", float(entry["limit_mag_5sigma"])), None
        else:
            return SubmissionDraft(**draft, content=None, blockers=[f"no calibrated magnitude or limit to report (result: {klass.replace('_', ' ')})"])
        if not findings.get("epoch_mid_iso"):
            return SubmissionDraft(**draft, content=None, blockers=["the stack has no observation time"])

        blockers: list[str] = []
        code = (self.settings.aavso_obs_code or "").strip()
        if not code:
            blockers.append("no AAVSO observer code is configured (set ASTRONOMO_AAVSO_OBS_CODE once the AAVSO has assigned one)")
        cp = xref.get("gaia_counterpart")
        notes = [
            "The star name is the TNS or LSST name; the AAVSO must already know the object (VSX) for WebObs to accept it: check before uploading.",
            "Magnitudes are on the Gaia DR3 G scale with no colour term, from an unfiltered (IR-cut) camera: coded CV with TRANS=NO. "
            "They are not V-band standard magnitudes.",
        ]
        if cp:
            notes.append(f"A Gaia DR3 star (G={cp['g_mag']:.1f}) lies {cp['separation_arcsec']:.1f} arcsec from the position: possibly a known variable or a blend.")
        stack = findings.get("stack") or {}
        zp_note = (
            f"Gaia G zero point from {calib.get('zp_n', '?')} stars (scatter {_fmt(calib.get('zp_scatter'), '.3f')}); "
            f"stack of {stack.get('n_frames', '?')} frames ({findings.get('total_exposure_s', '?')} s total); "
            f"classification vs Rubin: {klass.replace('_', ' ')}"
        )
        try:
            content = upload_formats.aavso_extended_report(
                obs_code=code or "XXXXX", software="Astronomo (rubin_followup)",
                rows=[{
                    "name": name, "jd": self._jd(findings["epoch_mid_iso"]), "mag": mag, "merr": merr, "filt": "CV",
                    "trans": False, "mtype": "STD", "cname": "ENSEMBLE", "chart": "Gaia DR3 G", "notes": zp_note,
                }],
            )
        except upload_formats.FormatError as exc:
            return SubmissionDraft(**draft, content=None, blockers=[f"could not be written in the AAVSO format: {exc}"])
        return SubmissionDraft(**draft, content=content, blockers=blockers, notes=notes)

    def detect_anomalies(self, mission: dict) -> list[dict]:
        """An outburst: the source is detected much brighter than Rubin's own light curve predicts.

        The significance is the brightening divided by one third of the agreement tolerance
        (``max(0.5 mag, 3 sigma)``), i.e. by ``max(0.17 mag, sigma)``: the band mismatch and missing colour term are
        included as a systematic floor rather than ignored, so a half-magnitude difference is 3 sigma, not 30.
        A source fading faster than predicted, or not detected, is not screened here: a moving object, an artefact
        or a pointing error explains those at least as well as physics does.
        """
        out: list[dict] = []
        for rec in mission.get("observations", []):
            target, xref = rec["target"], rec.get("crossref") or {}
            entry = (xref.get("entries") or [{}])[0]
            if entry.get("classification") != "detected_brighter_than_expected" or entry.get("delta_mag") is None:
                continue
            tol = float(entry.get("tolerance_mag") or MAG_TOLERANCE)
            exp = xref.get("rubin") or {}
            out.append({
                "kind": "outburst", "target": target["name"],
                "summary": (
                    f"{target['name']} is G~{entry['mag']:.2f}, {abs(entry['delta_mag']):.2f} mag brighter than Rubin's "
                    f"{exp.get('band', '?')}-band light curve predicts ({entry.get('expected_mag', float('nan')):.2f}) "
                    f"{exp.get('age_d', float('nan')):.1f} d after its last Rubin detection."
                ),
                "significance_sigma": abs(float(entry["delta_mag"])) / (tol / 3.0),
                "evidence": {
                    "mag": entry["mag"], "mag_err": entry.get("mag_err"), "expected_mag": entry.get("expected_mag"),
                    "delta_mag": entry["delta_mag"], "tolerance_mag": tol, "rubin_basis": exp.get("basis"),
                    "gaia_counterpart": bool(xref.get("gaia_counterpart")),
                },
            })
        return out
