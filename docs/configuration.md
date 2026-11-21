# Configuration

A simulation needs no settings. `python3 install.py` creates the environment and can write the upload identity, and `python main.py --simulate --fast` runs from a fresh checkout. The shipped [`.env.example`](../.env.example) leaves every identity line commented out.

Settings fall into three groups. **Simulation** needs none of them. **Before a real upload** is the observer name, the AAVSO and MPC codes, and the site. **Advanced** is everything else: the model, the telescope, an unattended night, and the plugin knobs. The example file carries the first two. This page carries all three.

## Where settings come from

Highest priority first:

1. **CLI flags**: `--llm-provider`, `--llm-model`, `--llm-base-url`, `--llm-strict`, `--llm-no-cache`, `--ollama`
2. **Real environment variables** (`export LLM_PROVIDER=...`)
3. **`.env`** in `astronomo/` (or the file named by `ASTRONOMO_DOTENV`; `ASTRONOMO_NO_DOTENV=1` ignores it)
4. **Defaults** in `config.py`

Every `LLM_*` setting can also be written `ASTRONOMO_LLM_*`. If both are set, the prefixed one wins. All other settings use the `ASTRONOMO_` prefix.

The dashboard writes one file, the observing site (`site.json`). It does not override the other settings. How that file ranks against the environment is under [The observing site](#the-observing-site).

## Needed for simulation

None. `--simulate` mocks the telescope and uses offline stand-ins for weather and catalogues. A language model stays off until you set one (see [Advanced](#advanced)).

## Needed before a real upload

These are identities the AAVSO and the MPC assign to you. Astronomo writes them into the upload files it prepares. It does not log in anywhere, send anything, or use them to choose targets. Leave a code empty until you have it: the file is still written, as a `DRAFT_` with that blocker. A code that is set and does not match the format stops startup, with the variable name and the rule.

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_OBSERVER_NAME` | empty | Submitter in the MPC ADES file (`asteroid_finder`), which is a `DRAFT_` until it is set. The AAVSO files do not use it |
| `ASTRONOMO_OBSERVER_INSTITUTION` | empty | Read into the settings but **not written into any upload file yet** |
| `ASTRONOMO_AAVSO_OBS_CODE` | empty | 1–5 letters or digits ([how to get one](runbooks/aavso-observer-code.md)). The `#OBSCODE=` line of the AAVSO files from `transit_finder`, `rubin_followup`, and `variable_stars` |
| `ASTRONOMO_MPC_STATION_CODE` | empty | Three letters or digits ([read this first](notes/mpc-observatory-code.md): a 50 mm telescope may not qualify). The station in the MPC ADES file from `asteroid_finder` |
| `ASTRONOMO_SITE_LAT` / `_SITE_LON` / `_SITE_ELEV_M` / `_SITE_NAME` | unset | The observing site ([below](#the-observing-site)). When set, they win over a saved site. A real-telescope run refuses the Denver placeholder |

`SIMULATED_` and `REPLAY_` files stay blocked whatever you set. Setting a code removes only its own "missing" blocker. What each code is written into is also in the [README](../README.md#quick-start).

## Advanced

### Language model

Optional. The default is no model. Setup steps are on the [model page](models.md#choose-a-model-local-cloud-or-none), and the four ready-to-uncomment blocks are in [`.env.example`](../.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_LLM_PROVIDER` | `none` | `none` \| `ollama` \| `openai` \| `anthropic` (`openai` also = any OpenAI-compatible server) |
| `ASTRONOMO_LLM_MODEL_NAME` | empty | Model; empty = `llama3.1` for Ollama, **required** for cloud providers |
| `ASTRONOMO_LLM_BASE_URL` | provider default | `http://localhost:11434` (Ollama), `https://api.openai.com/v1`, `https://api.anthropic.com` |
| `ASTRONOMO_LLM_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | unset | Cloud key (first one set wins; never logged or stored) |
| `ASTRONOMO_LLM_MAX_INPUT_CHARS` / `_MAX_OUTPUT_TOKENS` | `6000` / `400` | Hard per-call caps |
| `ASTRONOMO_LLM_MAX_CALLS_PER_MISSION` / `_MAX_TOKENS_PER_MISSION` | `8` / `20000` | Hard per-mission budget (persisted; survives restarts) |
| `ASTRONOMO_LLM_CACHE` | `true` | Answer identical requests from disk |
| `ASTRONOMO_LLM_NUM_CTX` | `4096` | Ollama context window (Ollama truncates silently if too small) |
| `ASTRONOMO_LLM_STRICT` | `false` | `true`: an LLM failure fails the mission (default: fall back to deterministic text) |
| `ASTRONOMO_LLM_TIMEOUT_S` / `_TEMPERATURE` | `180` / `0.0` | Read timeout / sampling temperature |

### Weather, an unattended night, and the interlocks

Self-start stays off. Nothing in the repository sets `ASTRONOMO_UNATTENDED_CONFIRMED`. The night-of commands are on the [real-hardware page](real-hardware.md).

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_CLOUD_THRESHOLD` | `30` | Cloud-cover % above which the mission sleeps |
| `ASTRONOMO_ASSUME_CLEAR` | `false` | When the forecast cannot be fetched, treat the sky as clear because the operator said so. A forecast that was actually read, including one saved by `--fetch-forecast`, is still enforced, and the Sun interlock still applies. Leave this off. Save the forecast before joining the telescope's own Wi-Fi ([real hardware](real-hardware.md)) |
| `ASTRONOMO_FORECAST_MAX_AGE_H` | `12` | How old a saved forecast may be and still stand in for a live reading. Older than this, a failed fetch is "not safe to observe" |
| `ASTRONOMO_MAX_WIND_KMH` | `40` | Wind limit |
| `ASTRONOMO_DEW_MARGIN_C` | `2` | Temperature minus dew point, from the forecast. Below this the mission waits. There is no dew sensor |
| `ASTRONOMO_MAX_SUN_ALT_DEG` | `-12` | Darkness interlock (real hardware only; `90` disables) |
| `ASTRONOMO_MIN_ALT_DEG` | `30` | Site interlock: lowest altitude a target may be observed at (real hardware only) |
| `ASTRONOMO_MAX_ALT_DEG` | `70` | Site interlock: highest altitude a target may be observed at. An alt-az mount turns the field fastest near the zenith (V0381 And at 81 degrees turned 1.76 degrees in nine minutes). Also applied by the Opportunities pages; `ASTRONOMO_VARSTAR_MAX_ALT_DEG` and `ASTRONOMO_TRANSIT_MAX_ALT_DEG` can only lower it for one plugin. `90` disables |
| `ASTRONOMO_MIN_SUN_SEP_DEG` | `30` | Site interlock: nearest a target may be to the Sun, whatever the darkness setting says |
| `ASTRONOMO_MOON_AVOID_DEG` / `_MOON_FLOOR_DEG` | `60` / `15` | Site interlock: while the Moon is up, the target must be at least `max(floor, avoid x illumination)` degrees from it (a full Moon needs 60, a half Moon 30, a thin one the floor) |
| `ASTRONOMO_INTERLOCKS_IN_SIMULATION` | `false` | Enforce the site interlocks (window, altitude, Sun, Moon, darkness) in simulation too, to rehearse them. Real hardware always enforces them |
| `ASTRONOMO_SLEEP_INTERVAL_S` / `_MAX_SLEEP_CYCLES` | `900` / `96` | Re-check cadence, and how many cloudy checks pass before the process is handed back with a wake time (site limits that never clear still end the mission) |
| `ASTRONOMO_MIN_FREE_GB` | `2` | Free space required where frames are written. A short disk stops the capture before the telescope slews |
| `ASTRONOMO_MIN_BATTERY_PCT` | `20` | Used when the telescope reports a battery. It does not, today. An unattended start refuses while the reading is missing; a person who confirms a start may go ahead |
| `ASTRONOMO_SELF_START` | `false` | With `--schedule`, start a due plan with nobody confirming it. Refused unless `ASTRONOMO_UNATTENDED_CONFIRMED` is also set |
| `ASTRONOMO_UNATTENDED_CONFIRMED` | `false` | The record that weather, Sun, Moon, altitude and safe parking have been seen on this mount. Nothing in the repository sets it. Self-start stays off until it is set, by the owner, after that night |
| `ASTRONOMO_SCHEDULE_POLL_S` | `60` | How often `--schedule` looks again. `--once` looks once and exits |
| `OPEN_METEO_API_KEY` | unset | Optional; switches to the commercial endpoint |

### The telescope

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_MOCK_MODE` | `true` | Telescope mock. `false` = **real hardware** |
| `ASTRONOMO_SEESTAR_HOST` / `_PORT` | `seestar.local` / `4700` | The telescope's host, used by both transports, as a name or an IP address. The port is the JSON-RPC control socket only (Alpaca has `_ALPACA_PORT` below). How to find the address, whether to reserve it in the router, and the `10.0.0.x` trap are in [Connect a real Seestar](connect-a-seestar.md) |
| `ASTRONOMO_SEESTAR_TRANSPORT` | `jsonrpc` | `jsonrpc` \| `alpaca`. Firmware 7.18 and newer needs `alpaca` (ASCOM Alpaca, no key); why is in [the firmware note](notes/firmware-and-authentication.md), and how to connect is in [Connect a real Seestar](connect-a-seestar.md) |
| `ASTRONOMO_SEESTAR_ALPACA_PORT` | `32323` | The Alpaca server's port (used only when the transport is `alpaca`) |
| `ASTRONOMO_SEESTAR_SHARE_PATH` | unset | Local mount of the Seestar image share (required for real capture on the JSON-RPC transport; not used over Alpaca) |
| `ASTRONOMO_SEESTAR_SET_SITE` | `true` | On connect, tell the Seestar the site and the time (from your site profile; a placeholder site is never sent) |
| `ASTRONOMO_SEESTAR_RETRIES` / `_SEESTAR_BACKOFF_S` | `3` / `2.0` | Transport failures (dropped Wi-Fi, reset or timed-out connection) reconnect, replay the set-up and retry this many times with exponential backoff. A device error reply is never retried |
| `ASTRONOMO_NODE_RETRIES` / `_NODE_BACKOFF_S` | `2` / `5.0` | A mission step that fails with a transient network or link error is run again this many times before the mission fails |
| `ASTRONOMO_FOCUS_WARN_FWHM_PX` / `_FOCUS_FAIL_FWHM_PX` | `6.0` / `10.0` | First-frame focus check on the star FWHM, in pixels. A fail stops the mission. **Guesses: re-set from real frames** |
| `ASTRONOMO_SEESTAR_FOCUSER_NUMBER` | `-1` | Alpaca device number of the focuser `python main.py --focus` moves. `-1` picks the one named Telephoto in the scope's own device list (the Wide Angle focuser is number 1) |
| `ASTRONOMO_AUTOFOCUS` / `_AUTOFOCUS_IN_SIMULATION` | `1` / `0` | Focus once per mission, after the first slew and before the first exposure: on a real telescope by default (`0` turns it off), in simulation only when asked (the mock focuser). A failure is logged (`kind=autofocus` in the mission log, `missions/<id>/autofocus.json`) and the mission goes on |
| `ASTRONOMO_FOCUS_EXPOSURE_S` / `_FOCUS_STEPS` | `10.0` / `7` | Focus (`--focus` and the mission): exposure of each test frame, and the number of focuser positions in one sweep window |
| `ASTRONOMO_FOCUS_HALF_WIDTH_FRAC` / `_FOCUS_MAX_WIDENINGS` | `0.02` / `2` | `--focus`: half the first sweep window, as a fraction of the range the focuser reports (or `--focus-half-width STEPS`), and how many times the window may widen toward the sharper side. **Guesses: the focuser's step size is not known** |
| `ASTRONOMO_FOCUS_MIN_IMPROVEMENT` | `0.15` | `--focus` keeps the new position only if a frame there is at least this fraction sharper than the start; otherwise the focuser goes back |
| `ASTRONOMO_FOCUS_TARGET_FWHM_PX` | `0` | Settle at the focuser position where the fitted star width reaches this many pixels, approached from below, instead of at the sharpest. `0` takes the sharpest. Off until the scatter has been compared on real frames (try `3.5`). `--focus-target PX` does the same for `--focus` |
| `ASTRONOMO_FRAME_WATCH` / `_FRAME_WATCH_IN_SIMULATION` | `1` / `0` | After every frame, measure the first frame's brightest stars (flux, sky, width, drift), print one line, log it, and keep `frame_stats.json` and a latest-frame preview for the dashboard. About 2 s a frame. In simulation only when asked |
| `ASTRONOMO_FRAME_WATCH_WARN_FRACTION` | `0.20` | The watch warns when the flux or the sky level moves this far from its rolling median. A slow fade over many frames is also reported |
| `ASTRONOMO_POINTING_RECENTRE_ARCMIN` | `0` | On the chip but further from the centre than this: slew once to centre the target. `0` never (the default): the scope's goto error is random and a correction can land as far away as the first slew |
| `ASTRONOMO_VARSTAR_COLOUR_TERM` | `on` | Variable-star magnitudes: fit the zero point against the comparison stars' Gaia BP-RP and apply the target's colour. `off` goes back to a plain median zero point |
| `ASTRONOMO_POINTING_WARN_ARCMIN` / `_POINTING_FAIL_ARCMIN` | `5.0` / `15.0` | First-frame pointing check (distance from the commanded position). A fail stops the mission. **Guesses: re-set from real frames** |
| `ASTRONOMO_SETUP_CHECKS_IN_SIMULATION` | `false` | Run the first-frame checks in simulation too, to rehearse them. They always run on a real telescope. The dashboard then shows that frame beside the result before the rest of the sequence |

### Plugins, archive replay, and the mission log

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_PLUGIN` | `variable_stars` | Active plugin (`--plugin` overrides) |
| `ASTRONOMO_ALLOW_DRAFT_PLUGINS` | `false` | `asteroid_finder` and `rubin_followup` are drafts, not ready for use. They run in simulation and on archive replays, and are refused on a real telescope unless this is set. `--list-plugins` shows each plugin's status |
| `ASTRONOMO_OFFLINE_SERVICES` | `false` | Offline weather/catalog stand-ins (`--simulate` sets this) |
| `ASTRONOMO_MAX_TARGETS` | `3` | Targets per mission |
| `ASTRONOMO_ANOMALY_MIN_SIGMA` | `5.0` | Significance an anomaly needs to be flagged in the mission log, enforced in core (`--anomaly-sigma` overrides) |
| `ASTRONOMO_NETWORK_CACHE_TTL_H` | `24` | How long ExoClock / Exoplanet Watch / NASA TTV lists are reused before a refetch (the MPC NEOCP is capped at 1 h). An expired copy is still used, flagged stale, if the fetch fails |
| `ASTRONOMO_DATA_DIR` | `./data` | Storage |
| `ASTRONOMO_API_HOST` / `_API_PORT` | `127.0.0.1` / `8765` | Bind address for `python -m dashboard.api`. The UI is http://localhost:8765 |
| `ASTRONOMO_API_CORS` | `*` | CORS origins for that API, comma-separated, or `*` |
| `ASTRONOMO_DATA_SOURCE` | `telescope` | `telescope` \| `archive` (replay a real public data set; `--data-source` overrides) |
| `ASTRONOMO_REPLAY_SPEED` | `0` | Archive replay: survey seconds per wall second. `0` = jump between visits, `1` = real time, `3600` = one survey hour per second |
| `ASTRONOMO_LEASE_TIMEOUT_S` | `45` | A mission whose owner has not heartbeated for this long is *orphaned* and may be taken over |
| `ASTRONOMO_ZTF_*` | see [The asteroid plugin](plugins/asteroid.md) | Archive mode: objects, search window, visits, frames, cutout size |
| `ASTRONOMO_ASTEROID_*` | see [The asteroid plugin](plugins/asteroid.md) | Plugin-specific (operator targets, NEOCP use and limits, min altitude, frames, interval) |
| `ASTRONOMO_TRANSIT_*` | see [The transit plugin](plugins/transit.md) | Plugin-specific (operator planet list, lookahead, baseline, frames, exposure, interval, and the assumed timing error for the Opportunities page) |
| `ASTRONOMO_RUBIN_*` | see [The Rubin follow-up plugin](plugins/rubin.md) | Plugin-specific (Fink tags, watchlist, brightness/age/reliability cuts, frames, exposure) |
| `ASTRONOMO_VARSTAR_*` | see [The variable-star plugin](plugins/variable_stars.md) | Plugin-specific (operator star list, lookahead, frames, exposure, interval, and the reachability limits for the Opportunities page) |

## The observing site

Everything that depends on where the telescope is (altitude, darkness, Sun and Moon distance, the Opportunities windows, weather) uses one site, resolved in this order:

1. `ASTRONOMO_SITE_LAT`, `ASTRONOMO_SITE_LON` (east positive), `ASTRONOMO_SITE_ELEV_M` and `ASTRONOMO_SITE_NAME`, if set.
2. `site.json` in the data directory, written by the dashboard's **Observing site** panel (Opportunities page). It is plain JSON: `{"name": "Back garden", "lat": 51.5, "lon": -0.12, "elev_m": 20.0, "horizon": [[90, 20], [180, 40]]}`.
3. A **placeholder** (Denver) so a fresh checkout runs. The dashboard warns while it is in use, and `main.py` refuses to start a mission on a real telescope until a site is set (simulation does not need one).

The panel can fill in coordinates from a place name (it sends only what you type to Open-Meteo's geocoding service when you press Search; check the result, since it gives the town's position and ground height, not yours). The optional **horizon** is a few `azimuth altitude` lines (azimuth from north through east): the lowest altitude you can see at each bearing, drawn as straight lines between the points. Where it is higher than `ASTRONOMO_MIN_ALT_DEG` it applies to the mission's interlocks, the Opportunities windows and the transit plugin's scheduling. The site stays on this computer.
