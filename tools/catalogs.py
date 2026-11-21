"""Catalogue wrappers: JPL Horizons ephemerides and Gaia DR3 cone searches.

Horizons goes through astroquery. Gaia DR3 is the same ``gaiadr3`` table, read from the
VizieR copy at the Harvard mirror. The ESA archive's job service times out often enough
that a finished night was left with no magnitude.

These are generic, reusable services.  Clients that serve a single science case (for example
the NASA Exoplanet Archive used by the transit plugin) live in that plugin instead.

Honest scope note: JPL Horizons answers "where is *this named object* at these times?",
it cannot list unknown objects inside a field.  Plugins therefore ask Horizons about the
objects they *expect* (the target and a watchlist) and treat anything unmatched as
"unidentified", never as "new".

Offline mode (``settings.offline_services``): Gaia answers from the synthetic sky the mock
telescope renders (:class:`tools.mock_sky.MockSky`, plus any extra stars the plugin's
:class:`~tools.mock_sky.SkyScene` declares) and tags every record ``source="simulated"``.
Offline queries need the target dict (``target=``) and the plugin's scene (``scene=``) so the
right synthetic sky can be rebuilt.  Horizons has no generic offline stand-in -- what a
simulated body does is science-case knowledge -- so a plugin that wants one provides it itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

from config import Settings
from tools.mock_sky import MockSky, SkyScene

log = logging.getLogger(__name__)


class CatalogError(RuntimeError):
    pass


# The Strasbourg votable endpoint currently returns 404, and the ESA TAP service times out.
# This mirror serves catalog I/355/gaiadr3, which is Gaia DR3.
VIZIER_GAIA_URL = "https://vizier.cfa.harvard.edu/viz-bin/votable"
VIZIER_GAIA_CATALOG = "I/355/gaiadr3"
VIZIER_TIMEOUT_S = 120
_GAIA_COLUMNS = ("Source", "RA_ICRS", "DE_ICRS", "Gmag", "BP-RP", "pmRA", "pmDE", "Plx", "e_Plx")


def _column(table, name: str) -> str:
    if name in table.colnames:
        return name
    raise CatalogError(f"VizieR Gaia table is missing {name} (columns: {list(table.colnames)})")


def _optional_float(value):
    """A catalogue number, or ``None`` when VizieR masked the cell."""
    if value is None:
        return None
    try:
        if np.ma.is_masked(value):
            return None
    except Exception:  # noqa: BLE001 - plain Python scalars have no mask
        pass
    number = float(value)
    if not np.isfinite(number):
        return None
    return number


def _vizier_gaia_table(ra_deg: float, dec_deg: float, radius_arcsec: float, g_mag_limit: float, max_rows: int):
    """The brightest Gaia DR3 sources in the cone, from the Harvard VizieR mirror.

    ``-sort=Gmag`` matters: VizieR stops at ``max_rows``, and the callers match the first
    stars in the list against the frame. An unsorted cap misses the stars on the chip.
    """
    from io import BytesIO

    import requests
    from astropy.io.votable import parse

    payload = [
        ("-source", VIZIER_GAIA_CATALOG),
        ("-c", f"{ra_deg:.6f} {dec_deg:+.6f}"),
        ("-c.rs", f"{float(radius_arcsec):.1f}"),
        ("-out.max", str(int(max_rows))),
        ("-sort", "Gmag"),
        ("Gmag", f"<{float(g_mag_limit)}"),
    ]
    payload.extend(("-out", column) for column in _GAIA_COLUMNS)
    try:
        response = requests.post(VIZIER_GAIA_URL, data=payload, timeout=VIZIER_TIMEOUT_S)
        response.raise_for_status()
        table = parse(BytesIO(response.content)).get_first_table().to_table()
    except Exception as exc:
        raise CatalogError(f"Gaia DR3 cone search via VizieR failed: {exc}") from exc
    return table


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class CatalogClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.offline = settings.offline_services
        self._horizons_cache: dict[tuple, list[dict]] = {}

    # ------------------------------------------------------------------ #
    # JPL Horizons
    # ------------------------------------------------------------------ #
    def horizons_ephemeris(
        self,
        designation: str,
        epochs: Sequence[datetime],
        location: str | None = None,
    ) -> list[dict[str, Any]]:
        """Topocentric ephemeris of a small body for the configured site.

        ``location`` is an MPC observatory code (e.g. ``"I41"`` = Palomar/ZTF) for archive data
        taken elsewhere; ``None`` uses the configured site.  Matters: a main-belt asteroid's
        parallax between sites is arcminutes.

        Returns one dict per epoch with ``ra_deg``, ``dec_deg``, ``rate_arcsec_per_min``,
        ``pa_deg``, ``v_mag``, ``delta_au`` (distance from the observer), ``r_au`` (distance
        from the Sun) and ``source``.
        """
        epochs = [_as_utc(e) for e in epochs]
        if self.offline:
            raise CatalogError(
                "JPL Horizons has no generic offline stand-in; a plugin that simulates "
                "bodies must supply its own ephemeris"
            )

        key = (designation, tuple(e.isoformat() for e in epochs), location)
        if key in self._horizons_cache:
            return self._horizons_cache[key]

        from astropy.time import Time
        from astroquery.jplhorizons import Horizons

        site: Any = location or {
            "lon": self.settings.site_longitude_deg,
            "lat": self.settings.site_latitude_deg,
            "elevation": self.settings.site_elevation_m / 1000.0,  # km
        }
        jd = [Time(e).jd for e in epochs]
        try:
            table = Horizons(id=designation, id_type="smallbody", location=site, epochs=jd).ephemerides(
                quantities="1,3,4,9,19,20"  # astrometric RA/DEC, rates, AZ/EL, V mag, r, delta
            )
        except Exception as exc:  # astroquery raises many unrelated types
            raise CatalogError(f"Horizons query for '{designation}' failed: {exc}") from exc

        out: list[dict[str, Any]] = []
        for row, epoch in zip(table, epochs):
            ra_rate = float(row["RA_rate"])  # arcsec/hr, already * cos(dec)
            dec_rate = float(row["DEC_rate"])  # arcsec/hr
            out.append(
                {
                    "designation": designation,
                    "epoch_iso": epoch.isoformat(),
                    "ra_deg": float(row["RA"]),
                    "dec_deg": float(row["DEC"]),
                    "rate_arcsec_per_min": float(np.hypot(ra_rate, dec_rate)) / 60.0,
                    "pa_deg": float(np.degrees(np.arctan2(ra_rate, dec_rate)) % 360.0),
                    "v_mag": float(row["V"]) if "V" in table.colnames and row["V"] is not None else None,
                    "delta_au": float(row["delta"]) if "delta" in table.colnames else None,  # distance from the observer
                    "r_au": float(row["r"]) if "r" in table.colnames else None,  # distance from the Sun
                    "elevation_deg": float(row["EL"]) if "EL" in table.colnames else None,
                    "source": "jpl-horizons",
                }
            )
        self._horizons_cache[key] = out
        return out

    # ------------------------------------------------------------------ #
    # Gaia DR3
    # ------------------------------------------------------------------ #
    def gaia_cone(
        self,
        ra_deg: float,
        dec_deg: float,
        radius_arcsec: float,
        g_mag_limit: float = 17.0,
        max_rows: int = 3000,
        *,
        target: dict | None = None,
        scene: SkyScene | None = None,
    ) -> list[dict[str, Any]]:
        """Gaia DR3 sources brighter than ``g_mag_limit`` inside the cone, brightest first.

        The rows are the VizieR copy of Gaia DR3 (Harvard mirror), not a query to the ESA archive.
        ``target`` / ``scene`` are only used offline, to rebuild the synthetic sky.
        """
        if self.offline:
            return self._simulated_gaia(ra_deg, dec_deg, radius_arcsec, g_mag_limit, target, scene)

        table = _vizier_gaia_table(ra_deg, dec_deg, radius_arcsec, g_mag_limit, max_rows)
        columns = {name: _column(table, name) for name in _GAIA_COLUMNS}
        rows = []
        for record in table:
            g_mag = _optional_float(record[columns["Gmag"]])
            ra = _optional_float(record[columns["RA_ICRS"]])
            dec = _optional_float(record[columns["DE_ICRS"]])
            source_id = record[columns["Source"]]
            if g_mag is None or ra is None or dec is None or np.ma.is_masked(source_id):
                continue
            if g_mag >= g_mag_limit:
                continue
            rows.append(
                {
                    "source_id": int(source_id),
                    "ra_deg": ra,
                    "dec_deg": dec,
                    "g_mag": g_mag,
                    "bp_rp": _optional_float(record[columns["BP-RP"]]),  # colour: red stars are larger
                    "pmra": _optional_float(record[columns["pmRA"]]),
                    "pmdec": _optional_float(record[columns["pmDE"]]),
                    "parallax_mas": _optional_float(record[columns["Plx"]]),
                    "parallax_err_mas": _optional_float(record[columns["e_Plx"]]),
                    "source": "gaia-dr3",
                }
            )
        rows.sort(key=lambda r: r["g_mag"])
        return rows[: int(max_rows)]

    def _simulated_gaia(
        self, ra: float, dec: float, radius_arcsec: float, g_limit: float, target: dict | None,
        scene: SkyScene | None,
    ) -> list[dict]:
        if target is None:
            raise CatalogError("offline Gaia needs target= to rebuild the synthetic sky")
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        cat = MockSky.from_target(target, scene=scene).star_catalog()
        idx = np.flatnonzero(cat["mag"] < g_limit)
        stars = SkyCoord(cat["ra"][idx] * u.deg, cat["dec"][idx] * u.deg)
        sep = SkyCoord(ra * u.deg, dec * u.deg).separation(stars).arcsec
        rows = [
            {
                "source_id": 9_000_000_000_000_000_000 + int(i),
                "ra_deg": float(cat["ra"][i]),
                "dec_deg": float(cat["dec"][i]),
                "g_mag": float(cat["mag"][i]),
                "pmra": 0.0,
                "pmdec": 0.0,
                "parallax_mas": None,
                "parallax_err_mas": None,
                "source": "simulated",
            }
            for i, s in zip(idx, sep)
            if s <= radius_arcsec
        ]
        rows.sort(key=lambda r: r["g_mag"])
        return rows
