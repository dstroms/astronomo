# The variable-star plugin

Measures variable stars for the AAVSO: one visit, one differential magnitude, and a file a person reviews. Why this case was chosen, the evidence behind the noise and colour claims, and the risks still open on a real unit are in [the feasibility note](variable-star-feasibility.md).

```bash
python main.py --plugin variable_stars --simulate --fast      # a compressed night, two stars
python main.py --plugin variable_stars --simulate             # the same, with real 10 s mock exposures
```

Simulation observes two fields: an eclipsing binary with a known minimum and magnitude ("V1234 Lac (sim)",
magnitude 11.3, a fractional depth of 0.28) and a steady control ("Control star (sim)", magnitude 11.8).
A successful run recovers the minimum time and both magnitudes, and writes an AAVSO Extended file for each.
The injected eclipse uses the same trapezoid the fitter looks for, so the shape agreement is optimistic in
the same way as the transit simulation. Nothing here has been run on a telescope.

1. **Targets.** Live, in order of precedence:
   1. `ASTRONOMO_VARSTAR_TARGETS`, if you set it: your own list of AAVSO star names (labelled `operator`).
   2. **The AAVSO Target Tool** (the default). Stars the AAVSO wants observed, with type, magnitude range,
      period, filter and cadence. Hosts that are only exoplanet targets are dropped. Each star is kept when
      a night in the lookahead is dark, high enough and clear of the Moon, using the same limits a mission
      uses. Ranked by how soon the night starts and by the list's own score.
   3. If the list cannot be read (and there is no cached copy), three built-in stars (`RR Lyr`, `SS Cyg`,
      `Z Cam`), tagged `manual`.

   A window from this list is a **usable night**, not an eclipse. The list gives a period and no time of
   minimum, and the plan says so. An eclipsing-binary plan says the night is not a time of minimum.
   Offline: the two simulated fields above. The simulated binary does carry a predicted minimum, because
   the simulation supplies one; a star taken from the AAVSO list does not.
2. **Observation.** One field. Live default: 40 frames of 10 s with 20 s between them (about 15 to 20
   minutes, enough for one magnitude). Simulated: 22 frames of 10 s with 480 s between them, so the
   compressed night samples the injected minimum.
3. **Reduction.** The transit photometry does the registration, apertures, comparison ensemble and
   trapezoid fit. A colour frame is reduced to its green channel first (`tools/imaging.py`), keeping the
   pixel grid so the world-coordinate solution still applies. A minimum is reported only when that fit
   clears the same thresholds a transit does (depth signal-to-noise at least 5, a BIC gain of 10, enough
   points in the dip and on either side). The night's magnitude is the median of the frames outside a
   fitted minimum, or of the whole series when there is none.
4. **Cross-reference.** The frame is shifted onto Gaia DR3, read from the Harvard VizieR mirror
   (`I/355/gaiadr3`) rather than the ESA archive. The measured star has to lie within 12 arcsec
   of the catalogue position. Comparison stars are Gaia sources within 8 arcsec of an aperture, and the
   target itself is excluded. The zero point is the median of `catalogue G + 2.5 log10(comparison flux)`.
   The reported magnitude is `zero point - 2.5 log10(target flux)`. A systematic floor of 0.03 mag is
   added in quadrature: there are no flat frames, and `TG` is not transformed to Johnson V.
   Classifications: `eclipse_on_time`, `eclipse_offset`, `eclipse_unpredicted`, `brighter_than_range`,
   `steady`, `identity_mismatch`, `uncalibrated`.
5. **Outputs.**
   - **Upload file** (the default): one **AAVSO Extended** line per star (`#TYPE=EXTENDED`, filter `TG`,
     `MTYPE=STD`, comparison `ENSEMBLE`, chart `Gaia DR3 G`). The time is the middle of the series, which
     is the brightness out of eclipse, not the time of minimum. A fitted minimum is described in the notes.
     A missing observer code or an unconfirmed identity is a blocker and the file is a draft. A visit with
     no magnitude writes no file. Simulated and replayed files stay `SIMULATED_` and `REPLAY_`. Astronomo
     never sends the file.
   - **Anomalies** proposed to the screen: `eclipse_timing` (the minimum differs from a predicted time;
     the error combines a 0.5 min floor, the fit and the prediction), `outburst` (brighter than the
     catalogued maximum, with the 0.03 mag floor) and `mira_offset` (a Mira-type star fainter than a
     predicted maximum, with a 0.3 mag floor). A measurement on a star that was not confirmed has
     significance 0. Core keeps only proposals at or above `ASTRONOMO_ANOMALY_MIN_SIGMA` (default 5).
   - A proposal at or above the threshold is flagged in the mission log (kind, target, significance and summary). A flag is a
     lead for a person to check, not a result. The light curve and the table of magnitude, minimum and classification are on the
     dashboard.

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_VARSTAR_TARGETS` | unset | Your own comma-separated AAVSO star names (live); replaces the ranked list |
| `ASTRONOMO_VARSTAR_LOOKAHEAD_H` | `36` | How far ahead to look for a night |
| `ASTRONOMO_VARSTAR_FRAMES` / `_EXPOSURE_S` / `_INTERVAL_S` | `40`/`10`/`20` live; `22`/`10`/`480` simulated | Frames, exposure, extra delay between frames |
| `ASTRONOMO_VARSTAR_MIN_ALT_DEG` | `30` | Minimum altitude across a night's window |
| `ASTRONOMO_VARSTAR_MAX_ALT_DEG` | `90` | Maximum altitude across a window. The lower of this and `ASTRONOMO_MAX_ALT_DEG` (default 70, the mission's own limit) applies: near the zenith an alt-az mount turns the field fastest |
| `ASTRONOMO_VARSTAR_COLOUR_TERM` | `on` | Fit the comparison stars' zero point against Gaia BP-RP and apply the target's colour (Gaia G is not the camera's green). On V0381 And the scatter fell from 0.14 to 0.04 mag and the night magnitude moved from 7.11 to 7.32 (Gaia G 7.33). `off` uses a plain median |
| `ASTRONOMO_VARSTAR_MAX_VMAG` / `_SATURATE_VMAG` | `13.0` / `7.0` | Skip a star that never reaches this brightness, or that saturates a 10 s frame even at its faintest |
| `ASTRONOMO_VARSTAR_MIN_WINDOW_MIN` | `60` | Shortest stretch of a night worth listing |
| `ASTRONOMO_VARSTAR_SIGMA_V10` | `0.032` | Opportunities page: assumed noise of one 10 s frame at V = 10. A placeholder, not measured on this telescope |

## What the night's magnitude can be trusted for

The magnitude is differential against Gaia G with a colour term, not a standard transformation: it is a camera green magnitude, reported untransformed. The findings carry a note when the magnitude drifts steadily over the sequence (a straight-line fit across the frames): a variable star can change in minutes, but a slope over a whole visit is more often the measurement. On 2026-11-16 the target gained 0.15 mag on its comparison stars while the sky faded by 40%, and brighter stars faded less than fainter ones; the cause is unproven. Judge the plugin by that night magnitude against the known answer, not by dip detection: a variable star is not a transit, and the minimum finder is only there for eclipsing stars.

## Opportunities

The dashboard's **Opportunities** page lists the same stars, with what this site can reach. A window shows
the proposed mission (that night's start and end) and how long it would run. It does not invent an event
time: the window is a usable night. **Plan this** saves a note. It does not start a mission. You start one
with `python main.py --from-plan ID` (add `--simulate` to rehearse; that leaves the plan alone). The plan
repeats the window note, including the sentence that an eclipsing-binary window is a usable night and not
an eclipse, and shows the same mission times.
