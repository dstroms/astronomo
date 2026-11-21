import contextlib
import copy
import inspect
import os
import sqlite3
from dataclasses import replace

import pytest

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplcfg-astronomo-tests")
os.environ["ASTRONOMO_NO_DOTENV"] = "1"  # a developer's own .env must never change test results

from config import get_settings  # noqa: E402
from core.graph import AstronomoRuntime, attach_db_logging, build_graph, detach_db_logging, run_mission  # noqa: E402
from core.llm import LLMClient  # noqa: E402
from core.state import initial_state  # noqa: E402
from db.database import Database  # noqa: E402
from plugins import load_plugin  # noqa: E402
from tools.catalogs import CatalogClient  # noqa: E402
from tools.telescope import SeestarController  # noqa: E402
from tools.weather import WeatherClient  # noqa: E402
from writer import LogWriter  # noqa: E402


def _offline(url, params, timeout):
    from core import networks

    raise networks.NetworkError(f"GET {url} blocked: tests never use the network")


@pytest.fixture(autouse=True)
def _no_network_target_lists(monkeypatch):
    """Pro-Am target lists (ExoClock, Exoplanet Watch, MPC NEOCP) are never fetched for real in tests.

    A test that wants list content patches ``core.networks._http_get_text`` itself; the default is an outage, which
    is also the interesting case for the fallback paths.
    """
    from core import networks

    monkeypatch.setattr(networks, "_http_get_text", _offline)


@contextlib.contextmanager
def offline_world(**env):
    """What the autouse fixture and ``monkeypatch`` give one test, for a *module-scoped* fixture.

    Module-scoped fixtures are built before any function-scoped fixture, so they would otherwise run with the network
    unblocked.  Use this around anything that runs a mission once for several tests.
    """
    from core import networks

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(networks, "_http_get_text", _offline)
        for key, value in env.items():
            mp.setenv(key, value)
        yield mp  # for a module fixture that also needs to patch something


def make_settings(data_dir):
    """The mock-mode, offline settings every test runs on (``settings`` fixture, and shared module fixtures)."""
    s = get_settings(
        data_dir=data_dir,
        mock_mode=True,
        offline_services=True,
        mock_capture_seconds=0.0,
        sleep_interval_s=0.0,
        max_sleep_cycles=5,
        mock_cloudy_checks=0,
        max_targets=3,
    )
    s.ensure_dirs()
    return s


@pytest.fixture()
def settings(tmp_path):
    return make_settings(tmp_path / "data")


class Harness:
    """Fully-wired runtime + compiled graph for tests."""

    def __init__(self, settings, plugin=None, llm=None):
        from langgraph.checkpoint.sqlite import SqliteSaver

        self.settings = settings
        self.db = Database(settings.db_path)
        self.llm_sleeps: list[float] = []  # retry back-offs the LLM client asked for (never really slept)
        if llm is None and settings.llm_provider != "none":
            llm = LLMClient(settings, sleep_fn=self.llm_sleeps.append)
        if llm is not None:
            llm.db = self.db
        self.llm = llm
        self.conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
        self.telescope = SeestarController(settings, sleep_fn=lambda s: None)
        self.captures = 0
        original = self.telescope.capture_sequence

        def counting(*a, **kw):
            self.captures += 1
            return original(*a, **kw)

        self.telescope.capture_sequence = counting  # type: ignore[method-assign]
        self.runtime = AstronomoRuntime(
            settings=settings,
            plugin=plugin or load_plugin("asteroid_finder", settings),
            telescope=self.telescope,
            weather=WeatherClient(settings),
            catalogs=CatalogClient(settings),
            writer=LogWriter(settings),
            db=self.db,
            sleep_fn=lambda s: None,
            llm=self.llm,
        )
        self.app = build_graph(self.runtime, SqliteSaver(self.conn))
        self._log_handler = attach_db_logging(self.db)  # same wiring as main.py

    def close(self):
        detach_db_logging(self._log_handler)
        self.conn.close()
        self.db.dispose()


@pytest.fixture()
def harness(settings):
    h = Harness(settings)
    yield h
    h.close()


# --------------------------------------------------------------------------- #
# Fast and slow tests
# --------------------------------------------------------------------------- #
#: Fixtures that run a whole mission (or the heavy image analysis behind one).  Any test that uses one, directly or through
#: another fixture, is marked ``slow`` automatically, so the shared run is never charged to a "fast" test.  A test that
#: runs a mission in its own body (``run_graph(...)``, ``run_mission(...)``) must say so with ``@pytest.mark.slow``.
HEAVY_FIXTURES = frozenset({"run_once", "harness", "sim", "transit_mission", "finished_mission", "llm_mission"})


#: Calls that run a whole mission; a test whose own body makes one is marked ``slow`` automatically as well.
_MISSION_CALLS = ("run_graph(", "run_mission(", "Harness(")


def _runs_a_mission(item) -> bool:
    try:
        return any(call in inspect.getsource(item.function) for call in _MISSION_CALLS)
    except (OSError, TypeError, AttributeError):
        return False


def pytest_collection_modifyitems(config, items):
    for item in items:
        if HEAVY_FIXTURES & set(item.fixturenames) or _runs_a_mission(item):
            item.add_marker(pytest.mark.slow)


# --------------------------------------------------------------------------- #
# Running a whole mission
# --------------------------------------------------------------------------- #
def run_graph(settings, plugin_name, mid="m_out", **overrides):
    """Run one whole mission; returns ``(final_state, node_sequence, artifacts, harness)`` (harness already closed)."""
    s = replace(settings, **overrides)
    h = Harness(s, load_plugin(plugin_name, s))
    try:
        h.db.create_mission(mid, plugin_name, True, {"cloud_cover_threshold_pct": 30.0})
        final = run_mission(h.app, mid, initial_state(mid, plugin_name, True))
        seq = [e["node"] for e in h.db.list_logs(mid, limit=3000, kinds=["node_start"])]
        arts = {k: h.db.list_artifacts(mid, k) for k in ("submission", "mission_log", "findings")}
        arts["observations"] = h.db.list_observations(mid)  # as stored for the dashboard
        return final, seq, arts, h
    finally:
        h.close()


@pytest.fixture(scope="module")
def run_once(tmp_path_factory):
    """Run a whole mission once per test module, however many tests read its result.

    A mission is real image processing (about 5 s for the asteroid plugin, 14 s for Rubin), so tests that only *read*
    one share it.  ``run_once(plugin, **overrides)`` returns ``(final, seq, artifacts, None)`` like ``run_graph``,
    as a private deep copy so one test can never change what another sees.  A test that patches the plugin or the
    environment, or that needs different settings, calls ``run_graph`` itself.
    """
    cache: dict = {}

    def get(plugin_name, **overrides):
        key = (plugin_name, tuple(sorted(overrides.items())))
        if key not in cache:
            settings = make_settings(tmp_path_factory.mktemp("shared_mission") / "data")
            with offline_world(ASTRONOMO_RUBIN_FRAMES="8"):
                final, seq, arts, _ = run_graph(settings, plugin_name, **overrides)
            cache[key] = (final, seq, arts)
        return (*copy.deepcopy(cache[key]), None)

    return get
