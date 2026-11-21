# Real hardware

Install and run a simulation with the [quick start](../README.md#quick-start) first (`python3 install.py`). [CONTRIBUTING](../CONTRIBUTING.md#getting-set-up) uses that command with `--non-interactive` so it asks nothing, then runs the tests. Read this page before setting `ASTRONOMO_MOCK_MODE=false`. The order for the first finished night is in [the first-mission note](first-light/06-first-mission.md).

- **JSON-RPC is tested only against a fake server.** `tools/telescope.py` implements the community-documented JSON-RPC
  protocol (CRLF-delimited, port 4700), cross-checked against the community's open-source projects but not
  against ZWO documentation. Method names live in one table (`_METHODS`) so firmware differences are a one-line
  fix. Framing, id matching, event skipping, error handling, retry and parking are covered by tests against a fake
  server, which proves *my* implementation is self-consistent, not that it matches your firmware. Do the first
  runs supervised. What this telescope has not yet answered is the [open hardware questions](first-light/05-first-light-checklist.md).
- **Smoke test first.** `python main.py --hardware-check` connects, reads the firmware and state, checks the
  location the scope holds against your site, checks the image share (JSON-RPC transport only) and parks (`--no-park` skips the park). It
  moves and captures nothing else, prints PASS / WARN / FAIL / INFO per step and saves the report to
  `data/hardware_checks/`. It sets the scope's site and time only when your site is not the placeholder.
- **Two transports; pick by firmware.** `ASTRONOMO_SEESTAR_TRANSPORT=jsonrpc` (the default) is the control protocol
  above. On **firmware 7.18 and newer it does not work**: it needs a client-authentication handshake that Astronomo
  does not implement on purpose. Set `ASTRONOMO_SEESTAR_TRANSPORT=alpaca` to use the standard ASCOM Alpaca server the
  firmware runs on port 32323, which needs no key (`tools/seestar_alpaca.py`). It returns frames directly,
  so no image share is needed, but it has no plate-solve call and does not select a filter. **On 2026-11-13 this drove
  a Seestar S50 Pro on firmware 9.75:** the firmware was updated in the app, Alpaca was switched on there, the computer
  joined the telescope's Wi-Fi, the hardware check passed, and a supervised capture slewed and saved 32 frames before
  the link dropped. On 2026-11-18 two Hat-P-23 visits finished on that telescope (12 frames each); the live AAVSO file
  was held because that Wi-Fi has no internet. On 2026-11-20 the same telescope was reached over a home network in
  station mode (next bullet). Why that handshake is not implemented is in
  [`first-light/03-firmware-and-authentication.md`](first-light/03-firmware-and-authentication.md).
  Firmware roughly 2583 to 2704 needs a `verify` parameter on the JSON-RPC transport, which the driver adds by itself.
- **Internet and the telescope together: station mode.** The telescope's own Wi-Fi has no internet, and a computer joined
  to it loses its other network, so weather and Gaia fail during a mission. In station mode the telescope joins your home
  Wi-Fi and the computer stays on that same network. Set it up in the Seestar app (Wi-Fi settings, Station Mode), switch the
  Alpaca server on again afterwards, and set `ASTRONOMO_SEESTAR_HOST` to the telescope's address; the README's
  [Connect a real Seestar](connect-a-seestar.md) has the steps. **Check the router first: a `10.0.0.x`
  range (the Xfinity default) makes station mode fail with "network connect failed".** ZWO blocks that range because
  the telescope's own Wi-Fi uses it. Moving the router to `192.168.1.x` fixed it here (2026-11-20). After that the
  scope answered at `192.168.1.200:32323`, Open-Meteo and the Harvard VizieR mirror answered from the same computer,
  and `--hardware-check --no-park` passed. `seestar.local` also worked as the host on this network (`--hardware-check` passed with it). Reserve the address in the router if it lets you (a rented gateway may not); otherwise use the name, and fall back to the address if the lookup is dropped. Also
  unplug any USB cable (the scope does not answer on the network while mounted as a drive). **Not yet seen:** a whole
  mission over station mode (frames, autofocus and park), and whether the scope keeps its address and the Alpaca
  setting through a power cycle. The scope's own Wi-Fi remains the fallback, with the forecast saved first (below).
- **Retries.** A dropped link reconnects, replays the set-up and retries (`ASTRONOMO_SEESTAR_RETRIES`, default 3,
  exponential backoff). A device error is never retried. A long wait sends a heartbeat every 8 s; whether that
  keeps the connection alive is unconfirmed.
- **Re-reducing a night.** `python main.py --reanalyze MISSION_ID` runs the reduction and the Gaia cross-reference again
  on a finished mission's saved frames, with the current code, and prints the night's magnitude. It changes nothing in the
  database and sends nothing.
- **Focus.** `python main.py --hardware-check` reads the Alpaca focuser's position and range (it moves nothing).
  `python main.py --focus` moves it: point the scope at a star field (or pass `--focus-at RA,DEC`), and it sweeps the
  focuser taking short frames, fits the star width, settles on the sharpest position and goes back to the start if
  the result is not clearly better. Stop it with Ctrl-C and it tries to return the focuser to where it began (if it cannot, it says so and where the
  focuser started: set it in the Seestar app). A mission runs the same sweep once before its first exposure on a real
  telescope (`ASTRONOMO_AUTOFOCUS=0` to skip it), logging each position's star width as `Autofocus:` lines and saving
  `missions/<id>/autofocus.json` (try that step alone with `python main.py --focus-step [--focus-at RA,DEC]`: same
  code and log lines, no mission); if it fails the mission carries on and the first-frame focus check decides. It needs the Alpaca
  transport, because the Seestar's own autofocus is behind the handshake Astronomo does not perform. **Unconfirmed
  on a Seestar:** the step size and range, whether the focuser moves while the scope tracks, the move time and the
  backlash. Watch the first run, and write the numbers in the [open hardware questions](first-light/05-first-light-checklist.md).
- **Per-frame watch.** After every frame a mission prints one line such as `Frame 7/40: flux 0.97 of frame 1, sky 0.95, FWHM 2.3 px, drift (+4, -2) px` (about 2 s of
  work; `ASTRONOMO_FRAME_WATCH=0` turns it off), writes a log row, and keeps `missions/<id>/frame_stats.json`, a plot and a
  latest-frame preview that the dashboard refreshes on its own. Cloud shows as flux and sky moving together; tracking trouble as
  width and position moving. It warns on a change of more than 20% from the rolling median and on a slow fade, so a sky that the
  weather lookup missed is seen while it happens. `python main.py --reanalyze` prints the same table for a finished night.
  Not yet seen on a real night. A target above `ASTRONOMO_MAX_ALT_DEG` (default 70) is refused before it slews.
- **Setup checks on the first frame** (focus FWHM, pointing against Gaia and the scope's own solve if it has one,
  background, noise, gradient and saturation) run on a real telescope and **stop the mission on a FAIL**; the
  report goes to the mission log. The thresholds are guesses made before the telescope arrived
  (`ASTRONOMO_FOCUS_*`, `ASTRONOMO_POINTING_*`) and must be re-set from real frames. A check that cannot be measured
  is a WARN, never a pass. **The dashboard shows that frame** beside the result as soon as it is on disk, and the
  rest of the sequence waits until the checks finish. The picture is the saved pixels, stretched so a tree, a roof,
  dew or cloud can be seen; too few stars stays a warning, and the checks still decide the hard stop. Open the
  mission: the picture sits above the sections while the run is live, and on Overview afterwards. Orientation
  against the Seestar app is unconfirmed (open hardware question 18). If the picture is not sky, stop with
  `python main.py --stop`. **Astronomo applies no darks or flats**: it records the frame statistics and leaves
  calibration to the Seestar's own processing. The transit photometry also measures field rotation and refuses to
  produce a light curve when it passes a limit.
- **FITS retrieval, JSON-RPC transport:** the control socket doesn't carry images. Mount the Seestar's image share
  locally and set `ASTRONOMO_SEESTAR_SHARE_PATH`; the controller collects new `*.fit(s)` files after each
  exposure and patches `OBJECT/RA/DEC/EXPTIME/DATE-OBS` into the headers if missing. Without the path, capture
  fails loudly. **Alpaca transport:** no share is used; each frame is one raw exposure downloaded from the camera
  and written with `OBJECT/RA/DEC/EXPTIME/GAIN/DATE-OBS` and a `TIMESRC` card saying whether the time came from the
  camera or this computer's clock.
- **Safety interlocks** (all in `check_weather`, rules in `core/interlocks.py`): cloud, wind, precipitation,
  unavailable weather data and Sun altitude, and for the target about to be observed: its **observing window**
  (`window_start`/`window_end`: a mission waits before it opens and skips the target after it closes), a minimum
  **altitude**, a minimum **Sun separation**, and **Moon separation** scaled by the Moon's illumination
  (see [configuration](configuration.md)). A target that fails a check other than a closed window is waited for like bad
  weather. Weather that outlasts `ASTRONOMO_MAX_SLEEP_CYCLES` hands the process back with a wake time, and
  `python main.py --schedule` resumes it. Site limits that never clear still end the mission. **These have been tested in simulation only
  (`ASTRONOMO_INTERLOCKS_IN_SIMULATION=1` rehearses them), never on a real mount.** The window is checked when a target
  starts, not mid-sequence, and the plugin sets it: `asteroid_finder` gives every target a two-hour window from the
  start of the mission, so later targets are skipped if a mission runs long. Dew uses the forecast's temperature and
  humidity (there is no dew sensor). Free space is checked before a capture. A battery percent is used only when the
  telescope reports one, which it does not today, and an unattended start will not guess. **Not implemented:** a
  horizon/obstruction mask (one minimum altitude stands in, or a horizon a person typed). The darkness interlock does not make
  unattended solar operation safe; you remain responsible for your optics.
- **Set your site first.** The interlocks and the weather are worked out for the configured site. Set it in the dashboard
  (Opportunities > Observing site) or with `ASTRONOMO_SITE_LAT`/`_SITE_LON`/`_SITE_ELEV_M`; a real-telescope run refuses to start while
  the site is the Denver placeholder. Add a horizon (a house, trees) there too if one hides part of the sky.
- **The forecast, before the telescope's own Wi-Fi.** That network has no internet (in station mode on a home network
  the live fetch works and this is only a fallback). Run `python main.py --fetch-forecast` while
  the computer still has internet. The file is `data/weather_forecast.json`. A mission uses the hour that covers "now" when the
  live fetch fails, for up to `ASTRONOMO_FORECAST_MAX_AGE_H` (default 12). That reading is enforced. Leave
  `ASTRONOMO_ASSUME_CLEAR` off.
- **Starting from a plan.** `python main.py --from-plan ID` re-checks a plan made on the Opportunities page and builds a
  mission that observes exactly that target (add `--simulate` to rehearse; a rehearsal leaves the plan alone).
  `python main.py --schedule --once` lists what is due and starts nothing. `--confirm ID` is the person saying yes.
  Self-start stays off until `ASTRONOMO_UNATTENDED_CONFIRMED` is set, after the interlocks and parking have been seen
  on this mount.
- **Stopping.** `python main.py --stop` parks the telescope and leaves the mission stopped. It will not resume.
  Ctrl-C still checkpoints and can be resumed. A telescope error already parks.
- **Seeing** is a heuristic proxy from jet-stream and surface wind, labelled as such everywhere. Open-Meteo
  has no seeing product.
