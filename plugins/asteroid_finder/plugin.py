"""Asteroid-finder experiment plugin (v1 example).

Science case: take short sequences of a field, look for point sources that move in a
straight line while the stars stay put, then ask whether the mover is a *known* object
whose JPL Horizons ephemeris explains it.

Everything asteroid-specific lives in this package; Astronomo core only talks to the
:class:`~core.base_plugin.BaseExperimentPlugin` interface.

Two data sources (``--data-source``)
    telescope   the Seestar (or its mock): one short sequence per field, now.
    archive     real Zwicky Transient Facility images from NASA/IPAC IRSA.  A mission is a list of
                *survey visits* of known asteroids (a few exposures minutes apart, visits days
                apart); the harness waits on the survey clock between them.  See ``ztf.py``.

Environment overrides (all optional)
    ASTRONOMO_ZTF_OBJECTS            archive: asteroids whose ZTF visits to replay (default "8191")
    ASTRONOMO_ZTF_BEGIN / _END       archive: search window, ISO dates (default 2019-01-10 / 2019-02-10)
    ASTRONOMO_ZTF_MAX_VISITS         archive: visits kept per mission (default 6)
    ASTRONOMO_ZTF_MAX_FRAMES         archive: exposures used per visit (default 12)
    ASTRONOMO_ZTF_CUTOUT_ARCMIN      archive: cutout side (default 6)
    ASTRONOMO_ZTF_MOST_URL / _IBE_URL  archive: service endpoints (tests point these at a fake server)
    ASTRONOMO_ASTEROID_TARGETS       comma list of small-body designations (live mode)
    ASTRONOMO_ASTEROID_MIN_ALT_DEG   minimum altitude to schedule a target (default 30)
    ASTRONOMO_ASTEROID_MAX_VMAG      faintest V magnitude to attempt (default 14.5)
    ASTRONOMO_ASTEROID_FRAMES        frames per target (default 4 simulated / 6 live)
    ASTRONOMO_ASTEROID_INTERVAL_S    delay between frames (default 0 simulated / 300 live)
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

from config import Settings
from core.base_plugin import BaseExperimentPlugin
from core.archive import ArchiveSource
from core.networks import TASK_ASTROMETRY, TASK_MANUAL, NetworkError, network_block
from core.submission import SubmissionDraft
from plugins.asteroid_finder import mpc, reduction, ztf
from tools.astrometry import (
    angular_separation_arcsec,
    estimate_translation,
    pixel_scale_arcsec,
    shift_wcs,
    sky_motion,
    target_altitude_deg,
    wcs_from_dict,
)
from tools import upload_formats
from plugins.asteroid_finder.scene import AsteroidScene
from core.archive import ArchiveError
from tools.catalogs import CatalogClient, CatalogError
from tools.mock_sky import MockSky

log = logging.getLogger(__name__)

DEFAULT_LIVE_TARGETS = "433,1036,4,3"  # Eros, Ganymed, Vesta, Juno: bright, well-known (the fallback when the NEOCP has nothing usable)
GAIA_G_LIMIT = 16.0
CALIBRATED_MATCH_RADIUS_ARCSEC = 30.0
UNCALIBRATED_MATCH_RADIUS_ARCSEC = 300.0
# Archive (ZTF): a 6' cutout holds only a handful of G<16 stars, so the check goes deeper; the
# survey's own WCS is astrometric to a fraction of an arcsecond, so the match radius is tight.
ARCHIVE_GAIA_G_LIMIT = 19.0
ARCHIVE_GAIA_MIN_MATCHES = 5
ARCHIVE_MATCH_RADIUS_ARCSEC = 10.0


def _env(name: str, default: str) -> str:
    return os.environ.get(f"ASTRONOMO_ASTEROID_{name}", default)


def _ztf_env(name: str, default: str) -> str:
    return os.environ.get(f"ASTRONOMO_ZTF_{name}", default)


class AsteroidFinderPlugin(BaseExperimentPlugin):
    name = "asteroid_finder"
    description = "Detects moving objects in short image sequences and cross-matches them with JPL Horizons."
    data_sources = ("telescope", "archive")
    status = "draft"
    status_note = "never run on a real telescope, and its upload needs an MPC observatory code that is not being pursued"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.simulated = settings.offline_services and not settings.archive_mode
        self._archive: ztf.ZTFArchive | None = None

    # ------------------------------------------------------------------ #
    # 1. Targets
    # ------------------------------------------------------------------ #
    def generate_targets(self) -> list[dict]:
        if self.settings.archive_mode:
            return self._ztf_targets()
        if self.settings.offline_services:
            return self._simulated_targets()
        return self._horizons_targets()

    # -- archive (ZTF) --------------------------------------------------- #
    def archive_source(self) -> ArchiveSource:
        if self._archive is None:
            self._archive = ztf.ZTFArchive(
                self.settings.archive_cache_dir,
                most_url=_ztf_env("MOST_URL", ztf.MOST_URL),
                ibe_url=_ztf_env("IBE_URL", ztf.IBE_SCI_URL),
                cutout_arcmin=float(_ztf_env("CUTOUT_ARCMIN", "6")),
                max_frames=int(_ztf_env("MAX_FRAMES", "12")),
            )
        return self._archive

    def target_limit(self) -> int:
        # A handful of telescope fields is a sensible cap; a survey replay's *visits* are the point.
        return int(_ztf_env("MAX_VISITS", "6")) if self.settings.archive_mode else super().target_limit()

    def _ztf_targets(self) -> list[dict]:
        source = self.archive_source()
        objects = [o.strip() for o in _ztf_env("OBJECTS", "8191").split(",") if o.strip()]
        begin, end = _ztf_env("BEGIN", "2019-01-10"), _ztf_env("END", "2019-02-10")
        targets: list[dict] = []
        errors: list[str] = []
        for obj in objects:
            try:
                found, stats = source.targets_for(obj, begin, end)  # type: ignore[attr-defined]
            except ArchiveError as exc:
                errors.append(f"{obj}: {exc}")
                log.warning("ZTF search for %s failed: %s", obj, exc)
                continue
            log.info(
                "ZTF cadence for %s: %d exposure(s) in %d visit(s); %d usable visit(s) of >=%d exposures",
                obj, stats["n_exposures"], stats["n_visits_total"], len(found), source.min_frames,  # type: ignore[attr-defined]
            )
            targets.extend(found)
        if not targets:
            raise ArchiveError(
                "no usable ZTF visits found" + (": " + "; ".join(errors) if errors else f" for {', '.join(objects)} in {begin}..{end}")
            )
        targets.sort(key=lambda t: t["not_before"])
        for i, t in enumerate(targets):  # keep the mission in survey order whatever the caller's sort does
            t["priority"] = 100.0 - i
        return targets

    def _simulated_targets(self) -> list[dict]:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        epoch0 = now.isoformat()
        window = {"window_start": now.isoformat(), "window_end": (now + timedelta(hours=2)).isoformat()}
        return [
            {
                "name": "433 Eros (sim)", "designation": "433", "ra_deg": 150.0, "dec_deg": 11.0,
                "priority": 3.0, **window,
                "sim": {"seed": 11, "epoch0_iso": epoch0, "pointing_error_arcsec": [28.0, -17.0],
                        "mover": {"dx_px": -70.0, "dy_px": 25.0, "vx_px_s": 0.25, "vy_px_s": -0.10, "mag": 14.3}},
            },
            {
                "name": "Field B (sim)", "designation": None, "ra_deg": 214.5, "dec_deg": -5.5,
                "priority": 2.0, **window,
                "sim": {"seed": 22, "epoch0_iso": epoch0, "pointing_error_arcsec": [-35.0, 22.0],
                        "mover": {"dx_px": 40.0, "dy_px": -30.0, "vx_px_s": -0.18, "vy_px_s": 0.12, "mag": 15.2}},
            },
            {
                "name": "Field C control (sim)", "designation": None, "ra_deg": 302.1, "dec_deg": 22.0,
                "priority": 1.0, **window,
                "sim": {"seed": 33, "epoch0_iso": epoch0, "pointing_error_arcsec": [12.0, 31.0], "mover": None},
            },
        ]

    def _horizons_targets(self) -> list[dict]:
        """Live targets: the MPC's NEO Confirmation Page first, then a built-in list of bright known asteroids.

        ``ASTRONOMO_ASTEROID_TARGETS`` (an operator's list) replaces both.  The NEOCP is where the MPC asks for follow-up
        astrometry, but almost every entry is far fainter than a 50 mm telescope can reach (see :mod:`plugins.asteroid_finder.mpc`),
        so on most nights it contributes nothing and the bright known asteroids are observed as a labelled ``manual`` fallback.
        """
        explicit = _env("TARGETS", "").strip()
        targets: list[dict] = [] if explicit else self._neocp_targets()
        if explicit:
            label, why = "operator", "chosen by the operator (ASTRONOMO_ASTEROID_TARGETS)"
        else:
            label, why = "built_in", "built-in list of bright, well-known asteroids; no network target list is driving this choice"
        targets += self._known_asteroid_targets(explicit or DEFAULT_LIVE_TARGETS, label, why, raise_if_none=not targets)
        return targets

    def _neocp_targets(self) -> list[dict]:
        """NEOCP entries a 50 mm telescope can point at tonight (usually none); failures only cost this source."""
        if _env("USE_NEOCP", "1").strip().lower() in ("0", "false", "no", "off"):
            return []
        s = self.settings
        max_v = float(_env("MAX_VMAG", "14.5"))
        max_unseen = float(_env("NEOCP_MAX_NOT_SEEN_D", "1.5"))
        min_alt = float(_env("MIN_ALT_DEG", "30"))
        try:
            rows, meta = mpc.fetch_neocp(s.archive_cache_dir, s.network_cache_ttl_h * 3600.0)
        except NetworkError as exc:
            log.warning("MPC NEO Confirmation Page unavailable (%s); using the built-in asteroid list instead.", exc)
            return []
        now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)
        candidates = mpc.usable(rows, max_v=max_v, max_not_seen_d=max_unseen, min_score=float(_env("NEOCP_MIN_SCORE", "0")))
        targets: list[dict] = []
        for row in candidates:
            alt = target_altitude_deg(row["ra_deg"], row["dec_deg"], s.site_latitude_deg, s.site_longitude_deg, s.site_elevation_m, now)
            if alt < min_alt:
                log.info("Skipping NEOCP %s: altitude %.0f deg < %.0f", row["temp_desig"], alt, min_alt)
                continue
            targets.append({
                "name": f"{row['temp_desig']} (NEOCP)", "designation": None, "neocp_desig": row["temp_desig"],
                "ra_deg": row["ra_deg"], "dec_deg": row["dec_deg"],
                "priority": 200.0 - row["v_mag"],
                "window_start": now.isoformat(), "window_end": (now + timedelta(hours=2)).isoformat(),
                "v_mag": row["v_mag"], "elevation_deg": alt, "position_epoch": "last observation (see network.reason)",
                "network": network_block(
                    [mpc.SOURCE], TASK_ASTROMETRY, mpc.reason(row, max_unseen), score=row["score"],
                    terms={"v_mag": row["v_mag"], "not_seen_d": row["not_seen_d"]},
                    retrieved_utc=meta.get("retrieved_utc"), stale_cache=bool(meta.get("stale")),
                ),
            })
        log.info(
            "MPC NEOCP: %d object(s) listed, %d bright enough (V <= %.1f) and recently seen (<= %.1f d), %d usable tonight.",
            len(rows), len(candidates), max_v, max_unseen, len(targets),
        )
        if not targets:
            vs = sorted(r["v_mag"] for r in rows if r["v_mag"] is not None)
            log.info(
                "MPC NEOCP offers nothing this telescope can measure (brightest entry V=%s); falling back to the built-in list.",
                f"{vs[0]:.1f}" if vs else "unknown",
            )
        return targets

    def _known_asteroid_targets(self, designations_csv: str, label: str, why: str, *, raise_if_none: bool) -> list[dict]:
        catalogs = CatalogClient(self.settings)
        designations = [d.strip() for d in designations_csv.split(",") if d.strip()]
        min_alt = float(_env("MIN_ALT_DEG", "30"))
        max_v = float(_env("MAX_VMAG", "14.5"))
        now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)

        targets: list[dict] = []
        errors: list[str] = []
        for des in designations:
            try:
                eph = catalogs.horizons_ephemeris(des, [now])[0]
            except (CatalogError, IndexError) as exc:
                errors.append(f"{des}: {exc}")
                log.warning("Skipping %s: %s", des, exc)
                continue
            alt, vmag = eph.get("elevation_deg"), eph.get("v_mag")
            if alt is not None and alt < min_alt:
                log.info("Skipping %s: altitude %.0f deg < %.0f", des, alt, min_alt)
                continue
            if vmag is not None and vmag > max_v:
                log.info("Skipping %s: V=%.1f fainter than %.1f", des, vmag, max_v)
                continue
            targets.append(
                {
                    "name": f"({des})", "designation": des,
                    "ra_deg": eph["ra_deg"], "dec_deg": eph["dec_deg"],
                    "priority": 100.0 - (vmag if vmag is not None else 15.0),
                    "window_start": now.isoformat(), "window_end": (now + timedelta(hours=2)).isoformat(),
                    "v_mag": vmag, "elevation_deg": alt,
                    "expected_rate_arcsec_per_min": eph["rate_arcsec_per_min"],
                    "network": network_block([label], TASK_MANUAL, why),
                }
            )
        if raise_if_none and not targets and errors and len(errors) == len(designations):
            raise CatalogError("every Horizons query failed: " + "; ".join(errors))
        return targets

    # ------------------------------------------------------------------ #
    # Simulation (offline only)
    # ------------------------------------------------------------------ #
    def mock_scene(self, target: dict) -> AsteroidScene:
        return AsteroidScene.from_target(target)

    def _ephemeris(self, catalogs: CatalogClient, designation: str, epochs: list[datetime], target: dict) -> list[dict]:
        """Horizons for real runs; this plugin's own simulated mover when offline."""
        if self.settings.archive_mode:  # the frames were taken at Palomar, not at the user's site
            return catalogs.horizons_ephemeris(designation, epochs, location=ztf.OBSERVATORY_CODE)
        if self.settings.offline_services:
            scene = self.mock_scene(target)
            return scene.ephemeris(MockSky.from_target(target, scene=scene), designation, epochs)
        return catalogs.horizons_ephemeris(designation, epochs)

    # ------------------------------------------------------------------ #
    # 2. Exposure plan
    # ------------------------------------------------------------------ #
    def get_exposure_plan(self) -> dict:
        live = not self.settings.offline_services
        return {
            "gain": 80,
            "exposure_s": 10.0,
            "filter": "IRCUT",
            "count": int(_env("FRAMES", "6" if live else "4")),
            # Real asteroids move arcsec/min: space frames out so motion exceeds a few px.
            "interval_s": float(_env("INTERVAL_S", "300" if live else "0")),
        }

    # ------------------------------------------------------------------ #
    # 3. Reduction
    # ------------------------------------------------------------------ #
    def analyze_frames(self, fits_paths: list[str]) -> dict:
        if not fits_paths:
            raise ValueError("analyze_frames() received no FITS files")
        findings = reduction.analyze(fits_paths, self._reduction_params(), figure_dir=Path(fits_paths[0]).parent)
        findings["plugin"] = self.name
        findings["data_source"] = self.settings.data_source
        return findings

    def _reduction_params(self) -> reduction.ReductionParams:
        if self.settings.archive_mode:
            # ZTF cutouts sit on a common sky grid with a solved WCS but show few stars: register by WCS.
            return reduction.ReductionParams(registration="wcs")
        return reduction.ReductionParams()

    # ------------------------------------------------------------------ #
    # 4. Cross-reference
    # ------------------------------------------------------------------ #
    def describe_hypothesis(self, targets: list[dict]) -> str:
        if self.settings.archive_mode:
            survey = (targets[0].get("survey") if targets else None) or {}
            first, last = (targets[0]["not_before"], targets[-1]["not_before"]) if targets else ("?", "?")
            return (
                f"Real ZTF images contain known asteroids that an unaided pipeline should recover as linear movers. "
                f"We replay {len(targets)} survey visit(s) between {first[:16].replace('T', ' ')} and "
                f"{last[:16].replace('T', ' ')} UTC (of {survey.get('n_exposures', '?')} ZTF exposures predicted to contain the "
                f"target asteroid, only visits of at least three exposures can show motion), detect movers without being told "
                f"where to look, and test each against JPL Horizons ephemerides computed for Palomar."
            )
        names = ", ".join(t["name"] for t in targets)
        return (
            f"Point sources that change position linearly across a short image sequence while the "
            f"stars remain fixed are solar-system objects. We observe {len(targets)} field(s) ({names}) "
            f"and test whether each detected mover is explained by a known body's JPL Horizons ephemeris."
        )

    def llm_guidance(self) -> str:
        return (
            "Domain: moving-object search in short image sequences. A 'track' is an unconfirmed candidate, "
            "never a discovery. 'unidentified_candidate' only means the track was not explained by the objects "
            "queried; it is not evidence of a new object. 'unverified' means no catalogue was consulted at all, so "
            "nothing can be said about the track's identity. 'quality' and 'snr' describe detection reliability. "
            "Photometry is instrumental (uncalibrated)."
        )

    def summarize_for_llm(self, findings: dict, crossref: dict | None = None, target: dict | None = None) -> dict:
        """Concise per-track JSON: reduction summary + cross-reference verdicts.  No pixels, no tables."""
        base = findings.get("agent_summary") or reduction.summarize(findings)
        out: dict[str, Any] = {"target": (target or {}).get("name"), **base}
        out["tracks"] = [dict(t) for t in base.get("tracks", [])]

        entries = {e.get("detection_id"): e for e in (crossref or {}).get("entries", [])}
        for track in out["tracks"]:
            entry = entries.get(track.get("id"))
            if not entry:
                continue
            track["classification"] = entry.get("classification")
            if entry.get("ra_deg") is not None and entry.get("dec_deg") is not None:  # Gaia-calibrated position
                track["ra_deg"], track["dec_deg"] = round(entry["ra_deg"], 4), round(entry["dec_deg"], 4)
            if entry.get("rate_arcsec_per_min") is not None:
                track["rate_arcsec_per_min"] = round(entry["rate_arcsec_per_min"], 2)
            match = entry.get("match")
            if match:
                track["match"] = match.get("designation")
                track["rate_consistent"] = match.get("rate_consistent")
                if entry.get("separation_arcsec") is not None:
                    track["separation_arcsec"] = round(entry["separation_arcsec"], 1)

        cal = (crossref or {}).get("calibration") or {}
        if cal:
            out["gaia_calibration"] = {
                k: (round(cal[k], 2) if isinstance(cal[k], float) else cal[k])
                for k in ("status", "offset_arcsec", "n_matches", "rms_arcsec") if k in cal
            }
        services = (crossref or {}).get("services")
        if services:
            out["services"] = {str(k): str(v) for k, v in services.items()}
        return out

    def cross_reference(self, findings: dict, catalogs: CatalogClient, target: dict) -> dict:
        detections = findings.get("detections") or []
        result: dict[str, Any] = {
            "summary": "",
            "entries": [],
            "services": {},
            "calibration": None,
            "notes": [],
        }
        if not detections:
            result["summary"] = "No moving candidates, nothing to cross-reference."
            return result

        base_wcs = wcs_from_dict(findings["astrometry"]["wcs"]) if findings.get("astrometry") else None
        if self.settings.archive_mode:
            # Survey frames come with an astrometric solution: trust it, and use Gaia only as a check.
            calibrated_wcs, calib = self._survey_astrometry(findings, catalogs, target)
            result["services"]["gaia_dr3"] = f"check only: {calib.get('gaia_check')}"
            wcs = base_wcs
            trusted = base_wcs is not None
            radius = ARCHIVE_MATCH_RADIUS_ARCSEC
        else:
            calibrated_wcs, calib = self._calibrate_with_gaia(findings, catalogs, target)
            result["services"]["gaia_dr3"] = calib.get("source") or calib["status"]
            wcs = calibrated_wcs or base_wcs
            trusted = calibrated_wcs is not None
            radius = CALIBRATED_MATCH_RADIUS_ARCSEC if trusted else UNCALIBRATED_MATCH_RADIUS_ARCSEC
        result["calibration"] = calib

        designations = [d for d in [target.get("designation"), *target.get("watchlist", [])] if d]
        horizons_state: str | None = None

        for det in detections:
            entry = self._classify(det, wcs, trusted, designations, catalogs, target, radius)
            service = entry.pop("_service", None)  # always strip the private key
            horizons_state = horizons_state or service
            result["entries"].append(entry)

        result["services"]["jpl_horizons"] = horizons_state or ("not queried (no designations)" if not designations else "unknown")
        known = sum(e["classification"] == "known_object_match" for e in result["entries"])
        unid = sum(e["classification"] == "unidentified_candidate" for e in result["entries"])
        unchecked = sum(e["classification"] == "unverified" for e in result["entries"])
        result["summary"] = (
            f"{len(detections)} candidate(s): {known} matched a known object, {unid} unidentified"
            + (f", {unchecked} not checked" if unchecked else "")
            + ". "
            f"Astrometry {calib['status']}"
            + (
                f" ({calib['n_matches']} Gaia stars, rms {calib['rms_arcsec']:.1f} arcsec)."
                if calib["status"] in ("calibrated", "survey_wcs") and "n_matches" in calib
                else "."
            )
        )
        if unchecked:
            result["notes"].append(
                f"{unchecked} candidate(s) could not be compared with a catalogue (see the notes on each entry: no "
                "designation or watchlist to query, no astrometric solution, or Horizons unavailable), so nothing "
                "is known about whether they are known objects."
            )
        if unid:
            result["notes"].append(
                "Horizons cannot enumerate objects in a field; 'unidentified' means only 'not explained by the "
                "objects queried'. Check the MPC (e.g. NEO Confirmation Page, MPChecker) before treating any "
                "candidate as new."
            )
        return result

    # -- helpers ---------------------------------------------------------- #
    def _survey_astrometry(self, findings: dict, catalogs: CatalogClient, target: dict):
        """Archive frames: keep the survey's own WCS and record how well Gaia agrees with it."""
        if not findings.get("astrometry"):
            return None, {"status": "unavailable", "reason": "no WCS in frames", "gaia_check": "not run"}
        shifted, check = self._calibrate_with_gaia(
            findings, catalogs, target, g_limit=ARCHIVE_GAIA_G_LIMIT, min_matches=ARCHIVE_GAIA_MIN_MATCHES
        )
        calib: dict[str, Any] = {"status": "survey_wcs", "source": "ZTF pipeline WCS", "gaia_check": check["status"]}
        if check["status"] == "calibrated":
            calib.update({k: check[k] for k in ("offset_arcsec", "n_matches", "rms_arcsec", "dx_px", "dy_px")})
        else:
            calib["gaia_check_reason"] = check.get("reason")
        return None, calib

    def _calibrate_with_gaia(
        self, findings: dict, catalogs: CatalogClient, target: dict, g_limit: float = GAIA_G_LIMIT, min_matches: int = 8
    ):
        astro = findings.get("astrometry")
        if not astro:
            return None, {"status": "unavailable", "reason": "no WCS in frames"}
        wcs = wcs_from_dict(astro["wcs"])
        ny, nx = astro["frame_shape"]
        scale = pixel_scale_arcsec(wcs)
        radius = 0.5 * float(np.hypot(nx, ny)) * scale + 300.0
        try:
            rows = catalogs.gaia_cone(
                float(wcs.wcs.crval[0]), float(wcs.wcs.crval[1]), radius, g_limit,
                target=target, scene=self.mock_scene(target),
            )
        except CatalogError as exc:
            log.warning("Gaia unavailable: %s", exc)
            return None, {"status": "unavailable", "reason": str(exc)}

        if len(rows) < min_matches:
            return None, {"status": "failed", "reason": f"only {len(rows)} Gaia sources in field"}
        gaia_px = np.array(wcs.all_world2pix([r["ra_deg"] for r in rows], [r["dec_deg"] for r in rows], 0)).T
        inside = (gaia_px[:, 0] > -50) & (gaia_px[:, 0] < nx + 50) & (gaia_px[:, 1] > -50) & (gaia_px[:, 1] < ny + 50)
        gaia_px = gaia_px[inside][:150]  # rows are sorted brightest first
        est = estimate_translation(
            gaia_px, np.asarray(astro["reference_stars"]), max_shift=150.0, tol=2.0, min_matches=min_matches
        )
        if est is None:
            return None, {"status": "failed", "reason": "no consistent star pattern match", "n_gaia": len(gaia_px)}

        new_wcs = shift_wcs(wcs, est["dx"], est["dy"])
        calib = {
            "status": "calibrated",
            "dx_px": est["dx"],
            "dy_px": est["dy"],
            "offset_arcsec": float(np.hypot(est["dx"], est["dy"]) * scale),
            "n_matches": est["n_matches"],
            "rms_arcsec": est["rms_px"] * scale,
            "source": rows[0]["source"],
        }
        log.info(
            "Gaia astrometric calibration: offset %.1f arcsec from %d stars (rms %.2f arcsec)",
            calib["offset_arcsec"], calib["n_matches"], calib["rms_arcsec"],
        )
        return new_wcs, calib

    def _classify(self, det, wcs, calibrated, designations, catalogs, target, radius) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "detection_id": det["id"],
            "calibrated": calibrated,
            "classification": "unidentified_candidate",
            "match": None,
            "separation_arcsec": None,
            "notes": [],
        }
        mid_t = datetime.fromisoformat(det["epoch_mid_iso"])
        if wcs is not None:
            ra, dec = wcs.all_pix2world([det["x_mid"]], [det["y_mid"]], 0)
            entry["ra_deg"], entry["dec_deg"] = float(ra[0]) % 360.0, float(dec[0])
            first, last = det["points"][0], det["points"][-1]
            p0 = wcs.all_pix2world([first["x"]], [first["y"]], 0)
            p1 = wcs.all_pix2world([last["x"]], [last["y"]], 0)
            dt_min = (datetime.fromisoformat(last["t_iso"]) - datetime.fromisoformat(first["t_iso"])).total_seconds() / 60.0
            entry["rate_arcsec_per_min"], entry["pa_deg"] = sky_motion(
                float(p0[0][0]), float(p0[1][0]), float(p1[0][0]), float(p1[1][0]), dt_min
            )
        else:
            entry["notes"].append("No WCS: cannot compare with ephemerides.")
            entry["classification"] = "unverified"
            return entry

        if not designations:
            # Nothing was asked of any catalogue, so "unidentified" (not explained by the objects queried) would
            # overstate it: no object was queried.
            entry["classification"] = "unverified"
            entry["notes"].append(
                "Not checked: the target has no designation or watchlist, so this track was not compared with any "
                "ephemeris. It is neither identified nor 'unidentified'."
            )
            return entry

        best: dict[str, Any] | None = None
        for des in designations:
            try:
                eph = self._ephemeris(catalogs, des, [mid_t], target)
            except CatalogError as exc:
                entry["notes"].append(f"Horizons unavailable for {des}: {exc}")
                entry["classification"] = "unverified"
                entry["_service"] = "unavailable"
                continue
            entry.setdefault("_service", eph[0]["source"] if eph else "no ephemeris")
            if not eph:
                continue
            sep = angular_separation_arcsec(entry["ra_deg"], entry["dec_deg"], eph[0]["ra_deg"], eph[0]["dec_deg"])
            if best is None or sep < best["separation_arcsec"]:
                best = {"designation": des, "separation_arcsec": sep, **eph[0]}

        if best is None:
            return entry
        entry["separation_arcsec"] = best["separation_arcsec"]
        if best["separation_arcsec"] <= radius:
            exp_rate, obs_rate = best["rate_arcsec_per_min"], entry["rate_arcsec_per_min"]
            rate_ok = exp_rate > 0 and abs(obs_rate - exp_rate) / exp_rate < 0.5
            entry["match"] = {
                "designation": best["designation"],
                "ephemeris_ra_deg": best["ra_deg"],
                "ephemeris_dec_deg": best["dec_deg"],
                "ephemeris_rate_arcsec_per_min": exp_rate,
                "ephemeris_v_mag": best.get("v_mag"),
                "ephemeris_delta_au": best.get("delta_au"),  # distance from the observer
                "ephemeris_r_au": best.get("r_au"),  # distance from the Sun
                "rate_consistent": bool(rate_ok),
                "source": best["source"],
            }
            entry["classification"] = "known_object_match" if rate_ok else "position_match_rate_mismatch"
            if not rate_ok:
                entry["notes"].append(
                    f"Position within {radius:.0f} arcsec of {best['designation']} but rate differs "
                    f"(observed {obs_rate:.1f}, expected {exp_rate:.1f} arcsec/min)."
                )
        else:
            entry["notes"].append(
                f"Nearest queried object ({best['designation']}) is {best['separation_arcsec']:.0f} arcsec away "
                f"(match radius {radius:.0f})."
            )
        return entry

    # ------------------------------------------------------------------ #
    # 5. Upload files (the default product; see core/submission.py)
    # ------------------------------------------------------------------ #
    #: Tracklets with fewer detected positions than this are not worth the MPC's time.
    MIN_ADES_POSITIONS = 3

    @staticmethod
    def _ades_ids(designation: str | None) -> tuple[str, str] | None:
        """``(permID, provID)`` for a published designation ("433" or "2019 AB12"); ``None`` if it is neither."""
        d = (designation or "").strip()
        if re.fullmatch(r"\d+", d):
            return d, ""
        if re.fullmatch(r"\d{4} [A-Z]{1,2}\d{0,3}", d):
            return "", d
        return None

    def result_notes(self, observation: dict) -> dict:
        """Candidates found, how well the frame was calibrated, and what each was matched to."""
        findings = observation.get("findings") or {}
        xref = observation.get("crossref") or {}
        calib = xref.get("calibration") or {}
        if calib.get("rms_arcsec") is not None and calib.get("n_matches"):
            error = (
                f"The astrometric calibration rms is {float(calib['rms_arcsec']):.2f} arcsec over {calib['n_matches']} Gaia "
                "stars. No magnitude is quoted: the photometry is instrumental."
            )
        else:
            error = "No astrometric error is quoted: the frame was not calibrated against Gaia. No magnitude is quoted either."
        return {"measured": findings.get("summary"), "error": error, "compared": xref.get("summary")}

    def caveats(self) -> list[str]:
        return [
            "This plugin is a draft and has not run on a real telescope. Treat everything here as a rehearsal.",
            "Positions are checked only against Gaia stars in the same frame. Photometry is instrumental, so no magnitude "
            "is reported.",
            "\"Unidentified\" means only \"not explained by the objects queried\". SkyBoT and the Minor Planet Center are "
            "not consulted, so it never means new.",
            "The MPC wants an observatory code and observations of faint objects. A 50 mm telescope may not meet its "
            "rules, and the ADES file should be run through the MPC's validator before anything is sent.",
        ]

    def build_submissions(self, mission: dict) -> list[SubmissionDraft]:
        """One MPC ADES file per observed field that has astrometry worth reporting.

        Only *detected* positions go in (a position the track predicted for a frame where the object was not found is
        an extrapolation, not a measurement), corrected by the Gaia calibration, with an uncertainty that combines the
        calibration's rms and the track's own scatter.  Magnitudes are left out: the photometry is instrumental.  An
        unidentified mover is held back, because the MPC should first be asked whether it is already known.
        """
        night = mission.get("night") or "unknown-night"
        return [self._ades_draft(rec, i, night) for i, rec in enumerate(mission.get("observations", []))]

    def _ades_draft(self, rec: dict, index: int, night: str) -> SubmissionDraft:
        target, findings, xref = rec["target"], rec.get("findings") or {}, rec.get("crossref") or {}
        name = target["name"]
        draft = dict(
            network="Minor Planet Center", kind="asteroid_astrometry", format="IAU ADES 2017 (PSV)", target=name,
            filename=f"{re.sub(r'[^A-Za-z0-9+.-]+', '', name)}_{night}_MPC_ADES.psv",
        )
        detections = findings.get("detections") or []
        if not detections:
            return SubmissionDraft(**draft, content=None, blockers=["no moving object was detected, so there is no astrometry to report"])
        calib = xref.get("calibration") or {}
        if calib.get("status") != "calibrated":
            why = (
                "positions come from the survey's own solution, whose reference catalogue is not recorded here"
                if calib.get("status") == "survey_wcs"
                else f"astrometry was not calibrated against Gaia (status: {calib.get('status', 'unknown')}); positions are only as good as the mount's pointing"
            )
            return SubmissionDraft(**draft, content=None, blockers=[why])
        catalogues = dict(upload_formats.ADES_CATALOGUES)
        if self.simulated:
            catalogues["simulated"] = "Gaia3"  # the offline stand-in mimics Gaia DR3; the file is SIMULATED_-prefixed regardless
        ast_cat = catalogues.get(str(calib.get("source")))
        if ast_cat is None:
            return SubmissionDraft(**draft, content=None, blockers=[f"the calibration catalogue {calib.get('source')!r} has no ADES code"])

        wcs = shift_wcs(wcs_from_dict(findings["astrometry"]["wcs"]), calib["dx_px"], calib["dy_px"])
        scale = pixel_scale_arcsec(wcs)
        entries = {e.get("detection_id"): e for e in xref.get("entries", [])}
        neocp = target.get("neocp_desig")
        observations: list[dict] = []
        skipped: list[str] = []
        if neocp:  # one NEOCP object, one tracklet: the longest track, never several movers under one temporary designation
            ranked = sorted(detections, key=lambda d: -sum(1 for p in d.get("points", []) if p.get("detected")))
            skipped += [f"{d['id']}: not reported; only the longest track in a NEOCP field is offered" for d in ranked[1:]]
            detections = ranked[:1]
        for det in detections:
            entry = entries.get(det["id"]) or {}
            if neocp:
                ids = ("", "")  # no designation yet: the MPC identifies the object by its NEOCP temporary designation
                trk = neocp
            else:
                matched = entry.get("classification") == "known_object_match"
                ids = self._ades_ids((entry.get("match") or {}).get("designation")) if matched else None
                if ids is None:
                    skipped.append(
                        f"{det['id']}: not reported ({entry.get('classification', 'unclassified').replace('_', ' ')}); "
                        "ask the MPC (MPChecker, the NEO Confirmation Page) whether it is already known before sending anything"
                    )
                    continue
                trk = f"AF{index:02d}{det['id']}"[:8]
            pts = [p for p in det.get("points", []) if p.get("detected")]
            if len(pts) < self.MIN_ADES_POSITIONS:
                skipped.append(f"{det['id']}: only {len(pts)} detected position(s); at least {self.MIN_ADES_POSITIONS} are needed")
                continue
            rms = max(math.hypot(float(calib.get("rms_arcsec") or 0.0), float(det.get("rms_px") or 0.0) * scale), 0.1)
            for p in pts:
                ra, dec = wcs.all_pix2world([p["x"]], [p["y"]], 0)
                when = datetime.fromisoformat(p["t_iso"])
                observations.append({
                    "perm_id": ids[0], "prov_id": ids[1], "trk_sub": trk,
                    "obs_time": when if when.tzinfo else when.replace(tzinfo=timezone.utc),
                    "ra_deg": float(ra[0]) % 360.0, "dec_deg": float(dec[0]), "rms_ra_arcsec": rms, "rms_dec_arcsec": rms,
                    "ast_cat": ast_cat,
                })
        if not observations:
            return SubmissionDraft(**draft, content=None, blockers=skipped or ["no track qualified for submission"])

        s = self.settings
        blockers: list[str] = []
        if not s.mpc_station_code:
            blockers.append("no MPC observatory code is configured (set ASTRONOMO_MPC_STATION_CODE once the MPC has assigned one)")
        if not s.observer_name:
            blockers.append("no observer name is configured (set ASTRONOMO_OBSERVER_NAME)")
        if neocp:
            blockers.append(
                f"the track has not been matched to {neocp}: the field can hold other movers and the NEOCP position is only good to "
                "about a field of view. Compare it with the MPC's ephemeris for the object before sending"
            )
        try:
            content = upload_formats.ades_psv(
                station=s.mpc_station_code or "XXX", submitter=s.observer_name or "UNKNOWN", observations=observations,
                telescope={"design": "refractor", "aperture": 0.05, "fRatio": 5.0, "detector": "CCD"},
                software="Astronomo (asteroid_finder)",
            )
        except upload_formats.FormatError as exc:
            return SubmissionDraft(**draft, content=None, blockers=[f"could not be written in ADES: {exc}"])
        notes = [
            "Written to the ADES 2017 PSV layout; check it with the MPC's PSV validator before submitting.",
            "Magnitudes are omitted: the pipeline's photometry is instrumental, and the MPC wants calibrated magnitudes or none.",
            "Seestar S50 is described as a 50 mm f/5 refractor with a CCD-mode detector, as other Seestar observers have reported to the MPC.",
            "Only detected positions are included; uncertainties are the Gaia calibration rms combined with the track's scatter.",
            *skipped,
        ]
        if neocp:
            notes.append(
                f"{neocp} has no designation yet: it is identified by its NEOCP temporary designation in trkSub. The MPC "
                "expects NEOCP follow-up to be sent promptly; a day-old file is of little use."
            )
        return SubmissionDraft(**draft, content=content, blockers=blockers, notes=notes)
