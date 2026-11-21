"""The Gaia cone is the Harvard VizieR copy of Gaia DR3, brightest stars first."""

from dataclasses import replace
from io import BytesIO

import numpy as np
import pytest
import requests
from astropy.io.votable import from_table
from astropy.table import MaskedColumn, Table

from tools.catalogs import VIZIER_GAIA_URL, CatalogClient, CatalogError


def _votable(rows: list[dict]) -> bytes:
    table = Table()
    for name in ("Source", "RA_ICRS", "DE_ICRS", "Gmag", "BP-RP", "pmRA", "pmDE", "Plx", "e_Plx"):
        values = [row[name] for row in rows]
        if any(value is None for value in values):
            data = [np.nan if value is None else value for value in values]
            mask = [value is None for value in values]
            table[name] = MaskedColumn(data, mask=mask)
        else:
            table[name] = values
    buffer = BytesIO()
    from_table(table).to_xml(buffer)
    return buffer.getvalue()


def test_the_gaia_cone_reads_the_brightest_stars_from_the_harvard_mirror(settings, monkeypatch):
    body = _votable([
        {"Source": 2, "RA_ICRS": 10.5, "DE_ICRS": -20.25, "Gmag": 14.0, "BP-RP": None,
         "pmRA": 1.5, "pmDE": -0.5, "Plx": None, "e_Plx": 0.2},
        {"Source": 1, "RA_ICRS": 10.1, "DE_ICRS": -20.2, "Gmag": 11.5, "BP-RP": 0.4,
         "pmRA": 3.0, "pmDE": 1.0, "Plx": 8.0, "e_Plx": 0.1},
        {"Source": 3, "RA_ICRS": 10.2, "DE_ICRS": -20.1, "Gmag": 16.2, "BP-RP": 1.0,
         "pmRA": 0.0, "pmDE": 0.0, "Plx": 1.0, "e_Plx": 0.3},
    ])
    captured = {}

    def post(url, data, timeout):
        captured["url"] = url
        captured["data"] = data
        captured["timeout"] = timeout

        class Response:
            content = body

            def raise_for_status(self):
                return None

        return Response()

    monkeypatch.setattr(requests, "post", post)
    rows = CatalogClient(replace(settings, offline_services=False)).gaia_cone(10.0, -20.0, 90.0, 16.0, max_rows=10)

    assert captured["url"] == VIZIER_GAIA_URL
    sent = dict(captured["data"])
    assert sent["-source"] == "I/355/gaiadr3"
    assert sent["-c"] == "10.000000 -20.000000"
    assert sent["-c.rs"] == "90.0"
    assert sent["-out.max"] == "10"
    assert sent["-sort"] == "Gmag"
    assert sent["Gmag"] == "<16.0"
    assert [row["source_id"] for row in rows] == [1, 2]
    assert rows[0]["g_mag"] == 11.5 and rows[0]["bp_rp"] == 0.4
    assert rows[0]["parallax_mas"] == 8.0 and rows[0]["source"] == "gaia-dr3"
    assert rows[1]["bp_rp"] is None and rows[1]["parallax_mas"] is None


def test_a_vizier_outage_is_a_catalog_error(settings, monkeypatch):
    def post(url, data, timeout):
        raise requests.ConnectionError("harvard unreachable")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CatalogError, match="via VizieR"):
        CatalogClient(replace(settings, offline_services=False)).gaia_cone(10.0, -20.0, 90.0, 16.0)
