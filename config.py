"""Central configuration for the Astronomo.

Everything tunable lives here and can be overridden with ``ASTRONOMO_*`` environment
variables.  Modules never read the environment directly; they receive a
:class:`Settings` instance (see :func:`get_settings`).  That keeps the core easy to
test and lets ``main.py --simulate`` override behaviour without mutating globals.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines from ``path`` into ``os.environ`` (no dependency needed).

    Real environment variables always win over the file.  Supports ``#`` comments, an optional
    ``export`` prefix and single/double-quoted values; malformed lines are ignored.  Set
    ``ASTRONOMO_NO_DOTENV=1`` to skip loading (the test suite does, so a developer's own ``.env``
    can never change test results).
    """
    if os.environ.get("ASTRONOMO_NO_DOTENV") or not path.is_file():
        return
    values: dict[str, str] = {}  # later lines override earlier ones, as in standard dotenv
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()  # trailing comment on an unquoted value
        values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)  # a real environment variable always wins


_load_dotenv(Path(os.environ.get("ASTRONOMO_DOTENV") or BASE_DIR / ".env"))  # ASTRONOMO_DOTENV: alternate file


def _raw(name: str) -> str | None:
    """``ASTRONOMO_<NAME>``, else (for LLM settings only) the bare ``<NAME>`` such as ``LLM_PROVIDER``."""
    value = os.environ.get(f"ASTRONOMO_{name}")
    if value is None and name.startswith("LLM_"):
        value = os.environ.get(name)
    return value


def _env(name: str, default: str) -> str:
    value = _raw(name)
    return default if value is None else value


def _env_bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


# --------------------------------------------------------------------------- #
# Module-level knobs (the "public" configuration surface)
# --------------------------------------------------------------------------- #

# Which plugin in ``plugins/`` drives the mission.  Override: ASTRONOMO_PLUGIN or --plugin.
ACTIVE_PLUGIN: str = _env("PLUGIN", "variable_stars")
# A plugin marked ``status = "draft"`` is not ready for use.  It runs in simulation and on archive replays, and is
# refused on a real telescope unless this is set.  Off by default; nothing in the repository sets it.
ALLOW_DRAFT_PLUGINS: bool = _env_bool("ALLOW_DRAFT_PLUGINS", False)

# Telescope mock mode.  True => no socket is ever opened and capture is simulated.
# Defaults to True so a fresh checkout can never move real hardware by accident.
MOCK_MODE: bool = _env_bool("MOCK_MODE", True)

# When True, weather and catalog lookups use deterministic offline stand-ins instead
# of Open-Meteo / JPL Horizons / Gaia.  ``main.py --simulate`` turns this on.
OFFLINE_SERVICES: bool = _env_bool("OFFLINE_SERVICES", False)

# Observing site.  Defaults are a placeholder -- set your own (the dashboard's Observing site panel, or these variables;
# see core/site.py).  A real-telescope mission refuses to start on the placeholder.
SITE_NAME: str = _env("SITE_NAME", "Home observatory")
SITE_LATITUDE_DEG: float = _env_float("SITE_LAT", 39.7392)
SITE_LONGITUDE_DEG: float = _env_float("SITE_LON", -104.9903)  # east positive
SITE_ELEVATION_M: float = _env_float("SITE_ELEV_M", 1609.0)

# Weather gating.  Cloud cover above the threshold sends the mission to SLEEP.
CLOUD_COVER_THRESHOLD_PCT: float = _env_float("CLOUD_THRESHOLD", 30.0)
# Operator assertion used when the forecast cannot be fetched (the telescope's own Wi-Fi has no internet).
# Off by default. It never overrides a forecast that was actually read, and the Sun interlock still applies.
ASSUME_CLEAR: bool = _env_bool("ASSUME_CLEAR", False)
# How long a forecast saved by ``--fetch-forecast`` may stand in for a live reading (the telescope Wi-Fi has no internet).
FORECAST_MAX_AGE_H: float = _env_float("FORECAST_MAX_AGE_H", 12.0)
MAX_WIND_KMH: float = _env_float("MAX_WIND_KMH", 40.0)
# Dew margin: temperature minus dew point, from the forecast's humidity. Below this, the mission waits.
DEW_MARGIN_C: float = _env_float("DEW_MARGIN_C", 2.0)
# Darkness interlock (real hardware only; ignored when MOCK_MODE is on).  The Sun must be
# below this altitude, otherwise the mission sleeps.  -12 deg = end of nautical twilight.
# Set to 90 to disable -- only do so if you know what your optics/filters can tolerate.
MAX_SUN_ALTITUDE_DEG: float = _env_float("MAX_SUN_ALT_DEG", -12.0)
# Site interlocks, checked before each target on real hardware (see core/interlocks.py).  A target that fails one is
# waited for (it may rise, or the Moon may set); a target whose own observing window has closed is skipped.
#   altitude  the target must be at least this high (a minimum altitude is the whole horizon model for now)
#   Sun       the target must be at least this far from the Sun, whatever the darkness setting says
#   Moon      while the Moon is up the target must be at least max(MOON_FLOOR, MOON_AVOID x illumination) degrees from it
MIN_TARGET_ALTITUDE_DEG: float = _env_float("MIN_ALT_DEG", 30.0)
# An alt-az mount turns the field fastest near the zenith (the first real night, at 81 deg, turned it
# 1.76 deg in nine minutes).  Above this a target is not started.  70 is a guess to re-set; 90 turns it off.
MAX_TARGET_ALTITUDE_DEG: float = _env_float("MAX_ALT_DEG", 70.0)
MIN_SUN_SEPARATION_DEG: float = _env_float("MIN_SUN_SEP_DEG", 30.0)
MOON_AVOID_DEG: float = _env_float("MOON_AVOID_DEG", 60.0)
MOON_FLOOR_DEG: float = _env_float("MOON_FLOOR_DEG", 15.0)
# The interlocks guard real optics, so they are ignored in simulation.  Set this to rehearse them there (tests do).
INTERLOCKS_IN_SIMULATION: bool = _env_bool("INTERLOCKS_IN_SIMULATION", False)
SLEEP_INTERVAL_S: float = _env_float("SLEEP_INTERVAL_S", 900.0)
MAX_SLEEP_CYCLES: int = _env_int("MAX_SLEEP_CYCLES", 96)
# Free space required where frames are written. An unattended start also refuses a battery the telescope does not report.
MIN_FREE_GB: float = _env_float("MIN_FREE_GB", 2.0)
MIN_BATTERY_PCT: float = _env_float("MIN_BATTERY_PCT", 20.0)
STORAGE_CHECK_IN_SIMULATION: bool = _env_bool("STORAGE_CHECK_IN_SIMULATION", False)
# The scheduler. Self-start is off until a person sets both, and only after the interlocks have been
# confirmed on this mount. The second flag is that confirmation; nothing in the repository sets it.
SELF_START: bool = _env_bool("SELF_START", False)
UNATTENDED_CONFIRMED: bool = _env_bool("UNATTENDED_CONFIRMED", False)
SCHEDULE_POLL_S: float = _env_float("SCHEDULE_POLL_S", 60.0)

# Mission shape.
MAX_TARGETS_PER_MISSION: int = _env_int("MAX_TARGETS", 3)

# --------------------------------------------------------------------------- #
# Pro-Am participation: where targets come from and what a mission produces
# --------------------------------------------------------------------------- #
# A mission's product is a set of upload files for the field's own database plus a mission log
# (Markdown).
# Significance a plugin-reported anomaly needs before it is flagged in the mission log.  Enforced in
# core, so a plugin cannot lower it.  Single-night amateur timing and photometry have unmodelled
# systematics, which is why this is stricter than the usual 3 sigma.
ANOMALY_MIN_SIGMA: float = _env_float("ANOMALY_MIN_SIGMA", 5.0)
# How long a downloaded network target list (ExoClock, Exoplanet Watch, MPC NEOCP ...) is reused
# before it is fetched again.  An expired copy is still used, with a warning, if the fetch fails.
NETWORK_CACHE_TTL_H: float = _env_float("NETWORK_CACHE_TTL_H", 24.0)

# Who the upload files are made out to.  None of these are secrets, but they are personal, so they
# default to empty and any upload file that needs one is written as a DRAFT until it is set.
OBSERVER_NAME: str = _env("OBSERVER_NAME", "").strip()
OBSERVER_INSTITUTION: str = _env("OBSERVER_INSTITUTION", "").strip()
AAVSO_OBSERVER_CODE: str = _env("AAVSO_OBS_CODE", "").strip()  # assigned by the AAVSO, max 5 characters
MPC_STATION_CODE: str = _env("MPC_STATION_CODE", "").strip()  # three characters, assigned by the MPC
# Same patterns as tools/upload_formats.py. Empty is legal: the upload file stays a DRAFT_.
_AAVSO_OBS_CODE = re.compile(r"^[A-Za-z0-9]{1,5}$")
_MPC_STATION = re.compile(r"^[A-Za-z0-9]{3}$")


def check_upload_identity(aavso_obs_code: str, mpc_station_code: str) -> None:
    """Refuse a code that is set and does not match the format. Empty is legal."""
    if aavso_obs_code and not _AAVSO_OBS_CODE.match(aavso_obs_code):
        raise ValueError(
            "ASTRONOMO_AAVSO_OBS_CODE must be 1-5 letters or digits, "
            f"not {aavso_obs_code!r}. Leave it empty until the AAVSO assigns one."
        )
    if mpc_station_code and not _MPC_STATION.match(mpc_station_code):
        raise ValueError(
            "ASTRONOMO_MPC_STATION_CODE must be three letters or digits, "
            f"not {mpc_station_code!r}. Leave it empty until the MPC assigns one."
        )

# Where frames come from.  "telescope" = the Seestar (or its mock); "archive" = replay a real
# public data set through the same pipeline (plugins that support it: asteroid_finder -> ZTF via
# IRSA).  Override: ASTRONOMO_DATA_SOURCE or --data-source.
DATA_SOURCE: str = _env("DATA_SOURCE", "telescope").strip().lower()
# Archive replay clock: survey-seconds that pass per wall-clock second.  0 = jump (waiting for
# the next visit costs no real time), 1 = real time, 3600 = one survey hour per second.
REPLAY_SPEED: float = _env_float("REPLAY_SPEED", 0.0)
# A mission is "owned" by the process that heartbeats it; a heartbeat older than this means the
# owner died and another process may resume the mission.
LEASE_TIMEOUT_S: float = _env_float("LEASE_TIMEOUT_S", 45.0)

# Open-Meteo (no key needed for the free tier; set one for the commercial endpoint).
OPEN_METEO_API_KEY: str | None = os.environ.get("OPEN_METEO_API_KEY") or None
OPEN_METEO_URL: str = _env("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
OPEN_METEO_COMMERCIAL_URL: str = "https://customer-api.open-meteo.com/v1/forecast"

# Seestar S50 (real hardware).
SEESTAR_HOST: str = _env("SEESTAR_HOST", "seestar.local")
SEESTAR_PORT: int = _env_int("SEESTAR_PORT", 4700)
# How to talk to the scope.  "jsonrpc": the community control protocol on SEESTAR_PORT (firmware
# 7.18 and later also demand a client-authentication handshake this driver does not perform).
# "alpaca": the standard ASCOM Alpaca REST server the firmware reportedly runs on
# SEESTAR_ALPACA_PORT, which needs no handshake and returns frames directly (no image share).
# Both are UNCONFIRMED against a real S50 until first light.
SEESTAR_TRANSPORT: str = _env("SEESTAR_TRANSPORT", "jsonrpc").strip().lower()
SEESTAR_TRANSPORTS = ("jsonrpc", "alpaca")
SEESTAR_ALPACA_PORT: int = _env_int("SEESTAR_ALPACA_PORT", 32323)
SEESTAR_TIMEOUT_S: float = _env_float("SEESTAR_TIMEOUT_S", 15.0)
# Local path of the Seestar's mounted SMB/HTTP-synced image share; FITS frames are
# collected from here after each exposure (see tools/telescope.py).
SEESTAR_SHARE_PATH: str | None = os.environ.get("ASTRONOMO_SEESTAR_SHARE_PATH") or None
# Tell the Seestar where and when it is (from the site profile) on connect, so its own
# goto / plate-solve start from the right place.  Turn off to leave the scope's own idea alone.
SEESTAR_SET_SITE: bool = _env_bool("SEESTAR_SET_SITE", True)
# Transient transport failures (dropped Wi-Fi, reset connection) are retried this many times
# with exponential backoff, reconnecting each time.  Device errors are never retried.
SEESTAR_RETRIES: int = _env_int("SEESTAR_RETRIES", 3)
SEESTAR_BACKOFF_S: float = _env_float("SEESTAR_BACKOFF_S", 2.0)
# Node-level retry (core/retry.py): a node that fails with a transient network or link error
# is run again this many times, with exponential backoff, before the mission is failed.
# First-frame setup checks (core/setup_checks.py).  Every number is a guess made before the
# telescope arrived: re-set them from the first real frames.
FOCUS_WARN_FWHM_PX: float = _env_float("FOCUS_WARN_FWHM_PX", 6.0)
FOCUS_FAIL_FWHM_PX: float = _env_float("FOCUS_FAIL_FWHM_PX", 10.0)
# Autofocus (tools/autofocus.py, ``python main.py --focus``): a sweep of the Seestar's Alpaca focuser.
# Nothing here has run on a physical Seestar.  The focuser's step size is not known, so the sweep is a
# fraction of the range it reports and widens toward the sharper side when the first window misses.
FOCUSER_NUMBER: int = _env_int("SEESTAR_FOCUSER_NUMBER", -1)  # -1: the telephoto focuser, found by name
# Focus once per mission, after the first slew and before the first exposure.  On by default on a real
# telescope; a failure is logged and the mission carries on (the first-frame focus check still decides).
AUTOFOCUS: bool = _env_bool("AUTOFOCUS", True)
AUTOFOCUS_IN_SIMULATION: bool = _env_bool("AUTOFOCUS_IN_SIMULATION", False)  # rehearse it with the mock focuser
# 3 s found no stars on the first real try (2026-11-16); the 10 s frames of that night held about 60.
FOCUS_EXPOSURE_S: float = _env_float("FOCUS_EXPOSURE_S", 10.0)
FOCUS_STEPS: int = _env_int("FOCUS_STEPS", 7)
FOCUS_HALF_WIDTH_FRAC: float = _env_float("FOCUS_HALF_WIDTH_FRAC", 0.02)  # of the focuser's reported range
FOCUS_MAX_WIDENINGS: int = _env_int("FOCUS_MAX_WIDENINGS", 2)
FOCUS_MIN_IMPROVEMENT: float = _env_float("FOCUS_MIN_IMPROVEMENT", 0.15)  # fraction of the starting FWHM
# A star width to settle at, not the sharpest the focuser reaches.  The first real sweep (2026-11-16) found
# a minimum of 2.19 px; at 2.2 px a Seestar star is a ring of four bright pixels and aperture photometry is
# at the mercy of where the star falls.  0 = take the sharpest (the default): nobody has yet compared the scatter
# at the two settings on real frames, and SeeVar, which has not defocused either, has no data on it.  Try 3.5.
FOCUS_TARGET_FWHM_PX: float = _env_float("FOCUS_TARGET_FWHM_PX", 0.0)
# Per-frame watch (core/frame_watch.py): flux, sky, width and drift of the first frame's bright stars after
# every frame, with a warning when they change.  About 2 s a frame (11 s on the first).
FRAME_WATCH: bool = _env_bool("FRAME_WATCH", True)
FRAME_WATCH_IN_SIMULATION: bool = _env_bool("FRAME_WATCH_IN_SIMULATION", False)
FRAME_WATCH_WARN_FRACTION: float = _env_float("FRAME_WATCH_WARN_FRACTION", 0.20)
POINTING_WARN_ARCMIN: float = _env_float("POINTING_WARN_ARCMIN", 5.0)
POINTING_FAIL_ARCMIN: float = _env_float("POINTING_FAIL_ARCMIN", 15.0)
# How far from the edge the target star must sit to count as on the chip, and how many
# corrected slews to try before refusing the night. A local solve-field, when installed,
# is hinted with the command and this search radius; the scale window comes from the frame.
POINTING_EDGE_MARGIN_PX: float = _env_float("POINTING_EDGE_MARGIN_PX", 64.0)
POINTING_MAX_NUDGES: int = _env_int("POINTING_MAX_NUDGES", 2)
# On the chip but further from its centre than this: slew once to put the target at the centre (0 = never, the
# default).  The 40-frame night drifted 400 px and the target started 34 arcmin off, so an off-centre start loses
# the target from the chip sooner.  But the Seestar's goto error is random, so a corrective slew can land as far
# away as the first one, and SeeVar reports that over-correcting hurt.  Turn it on only after seeing it help.
POINTING_RECENTRE_ARCMIN: float = _env_float("POINTING_RECENTRE_ARCMIN", 0.0)
POINTING_SOLVE_RADIUS_DEG: float = _env_float("POINTING_SOLVE_RADIUS_DEG", 5.0)
POINTING_SOLVE_TIMEOUT_S: float = _env_float("POINTING_SOLVE_TIMEOUT_S", 35.0)
# Run the checks in simulation too (to rehearse them); they always run on the real telescope.
SETUP_CHECKS_IN_SIMULATION: bool = _env_bool("SETUP_CHECKS_IN_SIMULATION", False)
NODE_RETRIES: int = _env_int("NODE_RETRIES", 2)
NODE_BACKOFF_S: float = _env_float("NODE_BACKOFF_S", 5.0)

# Mock capture behaviour.  The brief calls for a 10 s simulated capture per frame.
MOCK_CAPTURE_SECONDS: float = _env_float("MOCK_CAPTURE_SECONDS", 10.0)
# Number of initial weather checks that report heavy cloud in offline mode (demos SLEEP).
MOCK_CLOUDY_CHECKS: int = _env_int("MOCK_CLOUDY_CHECKS", 0)

# --------------------------------------------------------------------------- #
# Language model (optional; advisory prose only -- it never gates hardware or science)
# --------------------------------------------------------------------------- #
# "none" (default: zero tokens spent, deterministic text only), "ollama", "openai", "anthropic".
# "openai" also covers any OpenAI-compatible server (vLLM, LM Studio, llama.cpp, Ollama's /v1)
# when LLM_BASE_URL is set.
LLM_PROVIDER: str = _env("LLM_PROVIDER", "none").strip().lower()
# Empty => provider default for local Ollama ("llama3.1"); cloud providers must name a model.
LLM_MODEL_NAME: str = _env("LLM_MODEL_NAME", "").strip()
# Empty => provider default: Ollama http://localhost:11434, OpenAI https://api.openai.com/v1,
# Anthropic https://api.anthropic.com.
LLM_BASE_URL: str = _env("LLM_BASE_URL", "").strip()
# Explicit key override.  When unset, core/llm.py falls back to the provider's standard
# variable (OPENAI_API_KEY / ANTHROPIC_API_KEY) at client-build time, so CLI provider overrides
# pick up the right one.  Never stored in the DB or printed.
LLM_API_KEY: str | None = _raw("LLM_API_KEY") or None
LLM_TIMEOUT_S: float = _env_float("LLM_TIMEOUT_S", 180.0)  # local models are slow on first load
LLM_TEMPERATURE: float = _env_float("LLM_TEMPERATURE", 0.0)
# Token-waste guards.  The input cap is enforced on the *rendered* prompt; anything bigger is
# rejected before it is sent, not truncated (a truncated data summary would mislead the model).
LLM_MAX_INPUT_CHARS: int = _env_int("LLM_MAX_INPUT_CHARS", 6000)
LLM_MAX_OUTPUT_TOKENS: int = _env_int("LLM_MAX_OUTPUT_TOKENS", 400)
LLM_MAX_CALLS_PER_MISSION: int = _env_int("LLM_MAX_CALLS_PER_MISSION", 8)
LLM_MAX_TOKENS_PER_MISSION: int = _env_int("LLM_MAX_TOKENS_PER_MISSION", 20000)
# Identical requests are answered from disk (so a resumed node never pays twice).
LLM_CACHE: bool = _env_bool("LLM_CACHE", True)
# Ollama only.  Ollama's default context window is small and it TRUNCATES SILENTLY, so we set it.
LLM_NUM_CTX: int = _env_int("LLM_NUM_CTX", 4096)
# False: an LLM failure is recorded and the mission continues with deterministic text.
# True:  an LLM failure fails the mission (use when verifying a model integration).
LLM_STRICT: bool = _env_bool("LLM_STRICT", False)

# Storage.
DATA_DIR: Path = Path(_env("DATA_DIR", str(BASE_DIR / "data")))




@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of configuration passed to every component."""

    active_plugin: str = ACTIVE_PLUGIN
    allow_draft_plugins: bool = ALLOW_DRAFT_PLUGINS
    mock_mode: bool = MOCK_MODE
    offline_services: bool = OFFLINE_SERVICES

    site_name: str = SITE_NAME
    site_latitude_deg: float = SITE_LATITUDE_DEG
    site_longitude_deg: float = SITE_LONGITUDE_DEG
    site_elevation_m: float = SITE_ELEVATION_M
    #: Where the site came from: "default" (the placeholder), "environment" or "file" (``core/site.py``).
    site_source: str = "default"
    #: Lowest visible altitude by bearing, ``((azimuth, altitude), ...)``; empty = only ``min_altitude_deg`` applies.
    site_horizon: tuple = ()

    cloud_cover_threshold_pct: float = CLOUD_COVER_THRESHOLD_PCT
    assume_clear_sky: bool = ASSUME_CLEAR
    forecast_max_age_h: float = FORECAST_MAX_AGE_H
    max_wind_kmh: float = MAX_WIND_KMH
    dew_margin_c: float = DEW_MARGIN_C
    max_sun_altitude_deg: float = MAX_SUN_ALTITUDE_DEG
    min_altitude_deg: float = MIN_TARGET_ALTITUDE_DEG
    max_altitude_deg: float = MAX_TARGET_ALTITUDE_DEG
    min_sun_separation_deg: float = MIN_SUN_SEPARATION_DEG
    moon_avoid_deg: float = MOON_AVOID_DEG
    moon_floor_deg: float = MOON_FLOOR_DEG
    interlocks_in_simulation: bool = INTERLOCKS_IN_SIMULATION
    sleep_interval_s: float = SLEEP_INTERVAL_S
    max_sleep_cycles: int = MAX_SLEEP_CYCLES
    min_free_gb: float = MIN_FREE_GB
    min_battery_pct: float = MIN_BATTERY_PCT
    storage_check_in_simulation: bool = STORAGE_CHECK_IN_SIMULATION
    self_start: bool = SELF_START
    unattended_confirmed: bool = UNATTENDED_CONFIRMED
    schedule_poll_s: float = SCHEDULE_POLL_S
    max_targets: int = MAX_TARGETS_PER_MISSION

    anomaly_min_sigma: float = ANOMALY_MIN_SIGMA
    network_cache_ttl_h: float = NETWORK_CACHE_TTL_H
    observer_name: str = OBSERVER_NAME
    observer_institution: str = OBSERVER_INSTITUTION
    aavso_obs_code: str = AAVSO_OBSERVER_CODE
    mpc_station_code: str = MPC_STATION_CODE

    data_source: str = DATA_SOURCE
    replay_speed: float = REPLAY_SPEED
    lease_timeout_s: float = LEASE_TIMEOUT_S

    open_meteo_api_key: str | None = OPEN_METEO_API_KEY
    open_meteo_url: str = OPEN_METEO_URL
    open_meteo_commercial_url: str = OPEN_METEO_COMMERCIAL_URL

    seestar_host: str = SEESTAR_HOST
    seestar_port: int = SEESTAR_PORT
    seestar_transport: str = SEESTAR_TRANSPORT
    seestar_alpaca_port: int = SEESTAR_ALPACA_PORT
    seestar_timeout_s: float = SEESTAR_TIMEOUT_S
    seestar_share_path: str | None = SEESTAR_SHARE_PATH
    seestar_set_site: bool = SEESTAR_SET_SITE
    seestar_retries: int = SEESTAR_RETRIES
    seestar_backoff_s: float = SEESTAR_BACKOFF_S
    focus_warn_fwhm_px: float = FOCUS_WARN_FWHM_PX
    focus_fail_fwhm_px: float = FOCUS_FAIL_FWHM_PX
    focuser_number: int = FOCUSER_NUMBER
    autofocus: bool = AUTOFOCUS
    autofocus_in_simulation: bool = AUTOFOCUS_IN_SIMULATION
    focus_exposure_s: float = FOCUS_EXPOSURE_S
    focus_steps: int = FOCUS_STEPS
    focus_half_width_frac: float = FOCUS_HALF_WIDTH_FRAC
    focus_max_widenings: int = FOCUS_MAX_WIDENINGS
    focus_min_improvement: float = FOCUS_MIN_IMPROVEMENT
    focus_target_fwhm_px: float = FOCUS_TARGET_FWHM_PX
    frame_watch: bool = FRAME_WATCH
    frame_watch_in_simulation: bool = FRAME_WATCH_IN_SIMULATION
    frame_watch_warn_fraction: float = FRAME_WATCH_WARN_FRACTION
    pointing_recentre_arcmin: float = POINTING_RECENTRE_ARCMIN
    pointing_warn_arcmin: float = POINTING_WARN_ARCMIN
    pointing_fail_arcmin: float = POINTING_FAIL_ARCMIN
    pointing_edge_margin_px: float = POINTING_EDGE_MARGIN_PX
    pointing_max_nudges: int = POINTING_MAX_NUDGES
    pointing_solve_radius_deg: float = POINTING_SOLVE_RADIUS_DEG
    pointing_solve_timeout_s: float = POINTING_SOLVE_TIMEOUT_S
    setup_checks_in_simulation: bool = SETUP_CHECKS_IN_SIMULATION
    node_retries: int = NODE_RETRIES
    node_backoff_s: float = NODE_BACKOFF_S

    mock_capture_seconds: float = MOCK_CAPTURE_SECONDS
    mock_cloudy_checks: int = MOCK_CLOUDY_CHECKS

    llm_provider: str = LLM_PROVIDER
    llm_model_name: str = LLM_MODEL_NAME
    llm_base_url: str = LLM_BASE_URL
    llm_api_key: str | None = field(default=LLM_API_KEY, repr=False)  # keep out of reprs/logs
    llm_timeout_s: float = LLM_TIMEOUT_S
    llm_temperature: float = LLM_TEMPERATURE
    llm_max_input_chars: int = LLM_MAX_INPUT_CHARS
    llm_max_output_tokens: int = LLM_MAX_OUTPUT_TOKENS
    llm_max_calls_per_mission: int = LLM_MAX_CALLS_PER_MISSION
    llm_max_tokens_per_mission: int = LLM_MAX_TOKENS_PER_MISSION
    llm_cache: bool = LLM_CACHE
    llm_num_ctx: int = LLM_NUM_CTX
    llm_strict: bool = LLM_STRICT

    data_dir: Path = field(default_factory=lambda: DATA_DIR)

    def __post_init__(self) -> None:
        if self.seestar_transport not in SEESTAR_TRANSPORTS:
            raise ValueError(
                "ASTRONOMO_SEESTAR_TRANSPORT (seestar_transport) must be one of "
                f"{', '.join(SEESTAR_TRANSPORTS)}, not {self.seestar_transport!r}"
            )
        check_upload_identity(self.aavso_obs_code, self.mpc_station_code)

    @property
    def seestar_control_port(self) -> int:
        """The port the chosen transport talks to."""
        return self.seestar_alpaca_port if self.seestar_transport == "alpaca" else self.seestar_port

    # -- derived paths ------------------------------------------------------ #
    @property
    def db_path(self) -> Path:
        return self.data_dir / "astronomo.sqlite"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def missions_dir(self) -> Path:
        return self.data_dir / "missions"

    @property
    def llm_cache_dir(self) -> Path:
        return self.data_dir / "llm_cache"

    @property
    def archive_cache_dir(self) -> Path:
        """Downloaded archive products (calibration zips, ...) shared by every mission."""
        return self.data_dir / "archive_cache"

    @property
    def archive_mode(self) -> bool:
        return self.data_source == "archive"

    @property
    def autofocus_active(self) -> bool:
        """Whether a mission focuses before its first exposure: on a real telescope when enabled, in simulation on request."""
        if self.archive_mode:
            return False
        return self.autofocus_in_simulation if self.mock_mode else self.autofocus

    @property
    def frame_watch_active(self) -> bool:
        """Whether each frame is measured as it lands: on a real telescope when enabled, in simulation on request."""
        if self.archive_mode:
            return False
        return self.frame_watch_in_simulation if self.mock_mode else self.frame_watch

    @property
    def setup_checks_active(self) -> bool:
        """Whether the first-frame focus/pointing checks run: always on the real telescope, in simulation on request."""
        return (not self.mock_mode and not self.archive_mode) or self.setup_checks_in_simulation

    @property
    def storage_check_active(self) -> bool:
        """Free-space check before a capture: always on a real telescope, in simulation only when asked."""
        return (not self.mock_mode and not self.archive_mode) or self.storage_check_in_simulation

    @property
    def interlocks_active(self) -> bool:
        """Whether the site interlocks (darkness, altitude, Sun and Moon distance, observing window) are enforced.

        They guard real optics, so they apply whenever the telescope is real, and in simulation only when
        ``interlocks_in_simulation`` is set (to rehearse them).
        """
        return (not self.mock_mode) or self.interlocks_in_simulation

    @property
    def simulated(self) -> bool:
        """True only when *every* pixel and catalogue value is synthetic (never for archive data)."""
        return self.mock_mode and self.offline_services and not self.archive_mode

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.missions_dir.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, Any]:
        """Serialisable view for logging/DB (never includes secrets)."""
        return {
            "active_plugin": self.active_plugin,
            "allow_draft_plugins": self.allow_draft_plugins,
            "mock_mode": self.mock_mode,
            "offline_services": self.offline_services,
            "site": {
                "name": self.site_name,
                "lat": self.site_latitude_deg,
                "lon": self.site_longitude_deg,
                "elev_m": self.site_elevation_m,
                "source": self.site_source,
                "horizon": [list(p) for p in self.site_horizon],
            },
            "cloud_cover_threshold_pct": self.cloud_cover_threshold_pct,
            "assume_clear_sky": self.assume_clear_sky,
            "forecast_max_age_h": self.forecast_max_age_h,
            "dew_margin_c": self.dew_margin_c,
            "min_free_gb": self.min_free_gb,
            "min_battery_pct": self.min_battery_pct,
            "self_start": self.self_start,
            "unattended_confirmed": self.unattended_confirmed,
            "max_wind_kmh": self.max_wind_kmh,
            "interlocks": {
                "active": self.interlocks_active,
                "max_sun_altitude_deg": self.max_sun_altitude_deg,
                "min_altitude_deg": self.min_altitude_deg,
                "max_altitude_deg": self.max_altitude_deg,
                "min_sun_separation_deg": self.min_sun_separation_deg,
                "moon_avoid_deg": self.moon_avoid_deg,
                "moon_floor_deg": self.moon_floor_deg,
            },
            "max_targets": self.max_targets,
            "anomaly_min_sigma": self.anomaly_min_sigma,
            "data_source": self.data_source,
            "replay_speed": self.replay_speed,
            "llm": {
                "provider": self.llm_provider,
                "model": self.llm_model_name or None,
                "base_url": self.llm_base_url or None,
                "strict": self.llm_strict,
            },
        }


def get_settings(**overrides: Any) -> Settings:
    """Return the settings, optionally overriding individual fields."""
    settings = Settings()
    if overrides:
        settings = replace(settings, **overrides)
    if not any(key.startswith("site_") for key in overrides):  # an explicit site wins over the saved one
        from core.site import apply_site  # imported here: core.site needs Settings

        settings = apply_site(settings)
    return settings
