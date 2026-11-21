# The transit plugin

Measures exoplanet transits: one long observation of a single star, then a light curve.

```bash
python main.py --plugin transit_finder --simulate --fast      # ~1 min (about 30 frames per field)
python main.py --plugin transit_finder --simulate             # same, with real 10 s mock exposures (~10 min)
```

Simulation observes two fields: a known planet in transit ("WASP-12 b (sim)") and a star with no transit
(a control). The plugin's `TransitScene` injects a transiting host star plus the systematics a real night adds (pointing drift,
an atmospheric transparency trend, frame-to-frame cloud jitter, a target-only baseline slope), so the pipeline
has something to remove. A successful run ends with the planet confirmed against its (deliberately slightly wrong)
simulated catalogue entry and the control clean.

1. **Targets.** Live, in order of precedence:
   1. `ASTRONOMO_TRANSIT_TARGETS`, if you set it: your own list of NASA Exoplanet Archive names (labelled `operator`).
   2. **The Pro-Am networks' lists** (the default). [ExoClock](https://www.exoclock.space/database/planets_json) and the
      NASA Exoplanet Watch target list are merged by planet name, and the NASA Exoplanet Archive's `ttv_flag` marks planets
      with transit-timing variations. Each planet gets a score that rewards what the networks say they need most:

      `score = 1.0 * staleness + 0.75 * ExoClock priority + 1.0 * Exoplanet Watch rank + 1.5 * TTV flag`

      where `staleness = sigma_T / (T14 / 2)` is how uncertain the *predicted* transit time now is (propagated from the
      published period and mid-time errors, `sigma_T = sqrt((n dP)^2 + 2 n dP dT0 + dT0^2)` after `n` epochs, capped
      at 4) compared with half the transit duration. A planet whose schedule has drifted by a transit's length is the one
      an observation helps most. The ephemeris used is whichever list gives the smaller `sigma_T`. The tasks are
      `ephemeris_refinement` and, for TTV-flagged planets, `ttv_monitoring`. The score's terms are stored in
      `target["network"]["terms"]`, so a ranking can be checked by hand.
   3. If neither list can be read (and there is no cached copy), a short built-in list of bright hot Jupiters
      (`WASP-12 b, HAT-P-32 b, TrES-3 b, XO-1 b`), tagged `manual` and marked in the mission log as *not* following a network.

   Lists are cached for `ASTRONOMO_NETWORK_CACHE_TTL_H` (24 h); an outage uses the older copy and flags it stale. The next
   transit of each planet in the lookahead window is then computed (archive mid-times are BJD_TDB, converted to UTC with a
   barycentric correction of up to ~8 min), filtered by altitude, darkness, V magnitude and depth (Astronomo's own limits:
   ExoClock's 5-inch-class aperture floor would exclude a Seestar), and made non-overlapping, earliest first. Offline: two
   simulated fields.
   **See it before you start a mission.** The dashboard's **Opportunities** page shows the same lists with what this site can
   reach over the next 7, 14 or 30 days (see [Opportunities](#opportunities-what-could-i-take-part-in)).
2. **Observation.** One field, hundreds of frames (live default: 480 x 10 s at ~30 s cadence, about 4 h).
3. **Reduction** (`photometry.py`): pick the target (brightest star near the centre) and comparison stars, register
   frames, aperture photometry, **differential light curve** over the comparison ensemble (outlier stars rejected),
   then a **blind** trapezoid fit with a polynomial baseline. A transit is reported only if the depth SNR is at least
   5 (error bars inflated for excess and time-correlated noise), the BIC beats a no-transit baseline by 10, at least
   4 frames are in transit, and the quality/coverage flags say whether out-of-transit data bracket the dip.
4. **Cross-reference.** Mid-time offset (O-C), depth and duration against the catalogue; a Gaia DR3 check that the
   measured star is at the host's catalogue position. Classifications: `known_transit_confirmed`,
   `known_transit_timing_offset`, `known_transit_shape_mismatch`, `unexpected_dip`, `target_identity_mismatch`.
   A non-detection at an expected transit gets a **3-sigma depth upper limit**, or is reported as not covered.
5. **Outputs.**
   - **Upload file** (the default): one **AAVSO Exoplanet Report** (v0.62) per planet, with times in `BJD_TDB`, relative
     normalised flux (`Rnflux`, divided by the out-of-transit median when a transit was measured, otherwise by the series median and
     the notes say so; the current AAVSO guide lists only `Rnflux` and `Dmag`, not the old `Rflux`), filter `CV`, and the
     light-curve figure and a field image of the first frame (target marked T, comparison stars marked C) attached. The field image uses the pixel positions the light curve measured; it is not a new plate solution, and a host that Gaia did not confirm stays a draft. Control fields and fields with no light curve are held back (no file). A planet whose host star was not confirmed
     against Gaia gets a file with a blocker (`DRAFT_`), and so does any file made before `ASTRONOMO_AAVSO_OBS_CODE` is set. `#DETREND_PARAMETERS=` is left empty because the values are not
     detrended; the header is checked against the published specification, not against the AAVSO's validator.
   - **Anomalies** proposed to the screen: `timing_offset` (the mid-time differs from the ephemeris by N sigma, combining
     the fit's error with the ephemeris's own propagated error; this is the TTV signal), `missing_transit` (a well-covered
     expected transit is N sigma shallower than the catalogue depth) and `unexpected_dip` (a significant dip where none is
     expected). A measurement on a star not confirmed to be the host has significance 0. Depth and duration disagreement are
     deliberately not screened: aperture and blending systematics are not in the quoted errors.
   - A proposal at or above `ASTRONOMO_ANOMALY_MIN_SIGMA` is flagged in the mission log as a lead for a person to check, not
     a discovery. The fit and QA tables and the light curve per field are on the dashboard.

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_TRANSIT_TARGETS` | unset | Your own list of NASA Exoplanet Archive planet names (live); replaces the network lists. Unset = ExoClock + Exoplanet Watch + TTV flag, with `WASP-12 b,HAT-P-32 b,TrES-3 b,XO-1 b` only as the unreachable-network fallback |
| `ASTRONOMO_TRANSIT_LOOKAHEAD_H` | `24` | How far ahead to look for transits |
| `ASTRONOMO_TRANSIT_BASELINE_MIN` | `60` | Out-of-transit time wanted before and after |
| `ASTRONOMO_TRANSIT_MIN_ALT_DEG` | `30` | Minimum altitude across the window |
| `ASTRONOMO_TRANSIT_MAX_VMAG` / `_MIN_DEPTH_PCT` | `13.0` / `0.5` | Skip faint hosts / shallow transits (the Opportunities page uses the same limits) |
| `ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN` | `3.0` | Opportunities page only: the timing error assumed for one night's data, in minutes. A placeholder until a real run measures it |
| `ASTRONOMO_TRANSIT_FRAMES` / `_EXPOSURE_S` / `_INTERVAL_S` | `480`/`10`/`20` live; `30`/`10`/`470` simulated | Frames, exposure, extra delay between frames (simulated clock) |

## Opportunities: what could I take part in?

The Opportunities page in the bundled UI (`python -m dashboard.api`) answers, before
anything is started: which planets do the networks want timed, can this telescope reach them from this site, when, and what would a
timing be worth. It starts nothing and sends nothing; the one thing it writes is a *plan*, a note in the database.
A plan becomes a mission only when you run `python main.py --from-plan ID` (the Planned tab shows the exact commands; add
`--simulate --fast` to rehearse it first). Its status then follows the mission: *Planned*, *In progress*, *Done* or *Missed*
(the window passed, or the mission could not observe it), with a sentence saying how it ended. Before it starts, the plan is
re-checked: a closed window is refused, a moved schedule moves the window, and a poor sky is reported.

- **Your site.** The **Observing site** panel at the top sets where the windows are worked out for: coordinates (or a place search), and
  an optional horizon for a house or trees. Until a site is saved, the page warns that it is using a placeholder. The same planet list gives
  different answers at different places: a planet at declination -60 is out of reach from Denver and high in the sky from Canberra.
- **Where the data comes from.** The target lists already on this computer (`data/archive_cache/networks/`), so it opens instantly and
  never uses the network by surprise. The one exception is the very first open on a computer with no lists at all, when it downloads them once (one attempt per session) so the page is not empty. After that, **Refresh from networks** is the only thing that downloads. A list that could not be fetched
  is named, and an older copy used after a failed refresh is flagged.
- **Reachable** means the host is bright enough (`MAX_VMAG`), the transit deep enough (`MIN_DEPTH_PCT`), the planet climbs above
  `MIN_ALT_DEG` (or the site-wide `ASTRONOMO_MIN_ALT_DEG`, whichever is higher), and at least one transit in the next 30 days
  is **dark and high enough for the whole window** (the transit plus `BASELINE_MIN` either side, Sun below
  `ASTRONOMO_MAX_SUN_ALT_DEG`, far enough from the Sun and, while the Moon is up, from the Moon: the same limits a mission
  enforces on a real telescope). Weather is not known in advance and is not checked. Planets that fail are listed under *Cannot reach* with the
  reason, never hidden.
- **Kinds of observation.** When more than one plugin offers a list (exoplanet transits and variable stars today), a switch at the top, *Kind of observation*, picks one list. The other list is not further down the page. A single list has no switch. Open one directly with `?page=opportunities&case=variable_stars` (the plugin's name). Each choice carries a one-line summary of what that list is, and a count of how many targets this telescope can reach.
- **Views.** *Best next* (a card per planet: who asked, why, the next windows, the estimate, and a Plan this button). The chosen window shows the event (first to last contact: the predicted middle, minus and plus half the published duration) and the proposed mission (that event plus the baseline on each side) with the mission's length in hours, so the window is not read as the length of the dip. *Calendar*
  (nights, evening to morning, best-ranked first), *All reachable* (a table) and *Planned*. Sort by what the networks want (*Best*), by which next window is easiest to observe (bright, and high: a card says where the target is now, the highest point, how long it stays above 70 deg, and draws the altitude through the window), by the soonest window, or by the biggest estimated gain. Each time is followed by this computer's time zone; Technical also adds the UTC clock time.
- **Impact is an estimate, with its assumptions beside it.** For a window it combines the published schedule's uncertainty at that
  transit with one new timing of the assumed error (`ASSUMED_ERR_MIN`) and reports the uncertainty of a transit a year later, before
  and after. A schedule already known better than one night's data can improve is "Little gain"; a period with no stated error is
  "Not estimable" (any careful timing helps, but how much cannot be said). The telescope's real timing error is not measured yet, so
  the figure is a placeholder and says so. It never says anything about discovery. Under each estimate, the box *What this
  estimate assumes* has a button, *What is one timing worth?*, that opens a plain-language guide: one timing helps when a schedule is
  out of date and barely at all when it is well known, it cannot show a timing variation by itself, roughly how many timings
  that takes, and that a timing only counts if the networks accept it. Technical adds how the figures are worked out. The text lives
  in `plugins/transit_finder/opportunities.py` (`impact_guide`).
- **Is the schedule out of date?** Each card leads with a plain verdict and the date its published schedule is counted from.
  *Out of date*: the predicted transit time is uncertain by half the transit's length or more, so the transit could fall outside a
  normal window. *Up to date*: known about as well as one night's timing could do (`ASSUMED_ERR_MIN`), so a timing would barely
  change it (the same footing as "Little gain"). *Getting stale*: in between. *Can't tell*: the period has no stated error. The date
  is the transit the schedule is anchored on, **not** the last observation: the networks' lists do not say when a planet was last
  observed, and the page says so. The table has a *Schedule* column.
- **When a list is down.** If ExoClock cannot be read, a warning appears at the top of the page (not in the collapsed problems box):
  schedules then come from Exoplanet Watch and the NASA archive only, ExoClock's are usually fresher, so planets may look more out of
  date and a timing more valuable than they are. Those verdicts are marked *(provisional)* and each estimate's assumptions say so.
- **Not checked:** weather, the Moon, a real horizon or obstructions. Where the predicted time is less certain than the baseline
  taken either side, the window is marked *(time uncertain)* because the transit may fall outside it.
- **Plans.** *Plan this* stores the target, window and the estimate shown (`planned_observations`). The Planned tab repeats the event, the proposed mission and the mission's length. A plan is a note until you run `python main.py --from-plan ID` (add `--simulate --fast` to rehearse; that leaves the plan alone). The mission waits for the stored window; it does not start the moment you run the command.

**What the numbers mean (be careful):**

- In simulation the fit recovered an injected 1.4 % transit at 1.40 +/- 0.10 % (mean of 8 noise realisations, scatter
  equal to the quoted error), and 0 of 200 pure-noise light curves were flagged. The injected and fitted shapes are
  the same trapezoid, so this validates the photometry, detrending and statistics, not the physical realism of the model.
- Real transits are limb-darkened. A trapezoid's depth is a mean in-transit depth, not Rp/Rs squared.
- **Astronomo does not wait for the observing window.** Targets are ordered by start time and windows never overlap,
  but `check_weather` does not enforce `window_start`; start the mission when the first window opens (the log says when).
  Frames must also cover the transit: set `ASTRONOMO_TRANSIT_FRAMES`/`_INTERVAL_S` for the transit you chose.
- **Alt-az field rotation is measured and corrected, and the correction is unproven on a real sequence.** Each frame is registered to the first with a rotation about the frame centre plus a shift (`correct_rotation`, on by default), and the run warns past `warn_rotation_edge_px`. It was checked on simulated sequences rotated by up to 0.6 degrees and on the 40 frames of the first real night (about 0.7 degrees by the end, with a 400 px drift). Over hours the field still rotates far enough that comparison stars at the edge leave the frame, and the run reports that. With `correct_rotation=False` registration is translation-only and refuses a light curve past `max_rotation_edge_px`. The Gaia identity check assumes north-up frames and will say `unverified` on rotated frames.
- **Sharp stars on a colour mosaic are averaged 2x2 first.** The Seestar's frames carry no `BAYERPAT`, so the green channel cannot be picked out and the raw mosaic is measured. With 8.5 px stars that does not matter. After the first focused night (stars 2.2 px wide) the finder returned nothing at the target: a star landed as four bright pixels around a dead centre. When a Seestar frame's stars are narrower than 4 px (the first-frame check's width), every frame is averaged over a moving 2x2 window before measuring (`mosaic_averaging`: `auto`, `always` or `off`), and the warnings say so. The pattern was not guessed. The averaged pixels' photon-noise errors are too small; trust the light-curve scatter. The first-frame check has a `target star` line that tells you before the sequence whether a star is detected at the target.
- **Soft stars are measured with a wider finder.** The star finder starts at a 3 px width. When it finds fewer than 8 stars in the first frame, the run retries with wider kernels (4 to 14 px), scales the aperture and sky annulus to match, and says so in the warnings. On the first real night the stars were about 8.5 px wide and the 3 px finder found 4. A refocus gives a cleaner measurement.
- **A drifting mount is allowed up to 600 px of shift.** The first real night drifted about 12 px per frame (400 px over 40 frames). Frames that shift further than `max_registration_shift_px` from the first are excluded.
- **A series shorter than the shortest dip cannot be fitted.** A visit of about 25 minutes is shorter than the 30 minute minimum dip, so it gets a night's magnitude and no fitted minimum.
- Frame times are UTC mid-exposure. The planning step corrects the archive's BJD_TDB to UTC; the fit itself reports UTC.
  The upload file converts the light-curve times to BJD_TDB for the host's position (`tools.astrometry.utc_to_bjd_tdb_many`).
- **The four target lists have been fetched from the real services.** ExoClock (776 of 776 planets, 2026-11-14) and
  Exoplanet Watch (3030 planets, 2026-10-28) were parsed in full; trimmed copies of those files, the NASA TTV flag and
  the MPC NEOCP are the test fixtures. ExoClock's JSON has no TTV field, which is why the NASA flag is used.
- Live archive lookups were checked against the real service's column names, but a live end-to-end run (real
  telescope, real sky) has not been done.
