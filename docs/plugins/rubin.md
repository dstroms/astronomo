# The Rubin follow-up plugin

> **Draft: not ready for use.** This plugin has never run live (the Rubin alert stream was dark) and never on a real
> telescope. It is kept and tested, and runs in simulation. It refuses to command a real telescope unless
> `ASTRONOMO_ALLOW_DRAFT_PLUGINS=1` is set. Its mission logs say "DRAFT".

Takes targets from **public Vera C. Rubin alerts**, observes them with the Seestar, and checks the result against the
light curve Rubin itself measured.

```bash
python main.py --plugin rubin_followup --simulate --fast      # ~1 min, three simulated alerts
python main.py --plugin rubin_followup --simulate --live-services   # real Fink alerts, mock telescope
```

**What is public.** Rubin's *alert packets* are public with no proprietary period and are served by community brokers
(Fink, ALeRCE, Lasair, AMPEL, ANTARES, Babamul, Pitt-Google). Rubin's *images and catalogs* (DP1, DP2) are restricted to
data-rights holders, so this plugin uses alerts only. It reads the [Fink LSST REST API](https://api.lsst.fink-portal.org)
(no login; small read-only queries only, it is not a bulk-download service). Each alert carries a difference-image flux,
and `/api/v1/sources` returns an object's earlier alert detections, which is the light curve.

**The alert stream can be dark.** It stopped around 2026-07-14 and was expected to resume in mid/late October 2026
(see <https://ls.st/alert-stream-status>). The plugin only takes alerts newer than `ASTRONOMO_RUBIN_MAX_AGE_D`; if the stream
is stale it stops with an explicit message naming the newest alert it saw, rather than observing old transients. You
can still follow named objects with `ASTRONOMO_RUBIN_OBJECT_IDS` (Rubin `diaObjectId`s), which skip the brightness and age cuts.

1. **Targets.** Live: recent alerts for each Fink tag are merged to one row per object and filtered, in order: positive
   detection, real/bogus reliability, brighter than `MAX_MAG`, newer than `MAX_AGE_D`, above `MIN_ALT_DEG` for the next hour.
   Brightest first. If nothing qualifies, the error says how many alerts failed each cut. Offline: three simulated alerts
   (a fading supernova, one that has vanished, one that has brightened).
2. **Observation.** Short exposures of the alert position, stacked later.
3. **Reduction** (`photometry.py`, sees only FITS files): register frames (integer-pixel shifts), repair hot pixels, drop
   cloudy frames, mean-stack, detect sources. It writes `stack.fits` and a source list, and knows nothing about the target.
4. **Cross-reference** (sees the target): fit the pointing error against Gaia DR3, derive a photometric zero point
   `G = ZP - 2.5 log10(flux_adu)` (no colour term), and run **forced aperture photometry** at the alert position
   with an empirical noise floor. The expected magnitude comes from Rubin's own light curve: its last point, or a
   linear trend (at least 3 points over at least 0.5 d), extrapolated only if the age is within 2x the baseline and
   the slope is at most 1.5 mag/d. Agreement means `|delta| <= max(0.5 mag, 3 sigma)`. Classifications:
   `detected_consistent_with_rubin`, `detected_brighter_than_expected`, `detected_fainter_than_expected`,
   `detected_no_rubin_reference`, `marginal_detection`, `not_detected_expected_visible`, `not_detected_uninformative`
   (the limit is too shallow to say anything), `not_detected`, `*_uncalibrated`, `not_covered`, `not_measured`.
5. **Outputs.**
   - Each target carries a `network` block: source `fink_lsst` (plus `operator` for a watchlist object), task
     `transient_followup`, and a one-sentence reason (band, brightness, age, reliability, TNS name).
   - **Upload file** (the default): one **AAVSO Extended** (v1.2) line per alert, with a magnitude if the source was
     detected, or a fainter-than limit (`<18.6`) if not. The magnitudes are on the **Gaia G scale with no colour term**, from
     an IR-cut clear filter: they are coded `CV`, `TRANS=NO`, `CHART=Gaia DR3 G`, and the notes say they are not V-band standard
     magnitudes and give the zero-point stars and scatter. The AAVSO must already know the object (VSX) for WebObs to
     accept it, which a person has to check. It is a `DRAFT_` until `ASTRONOMO_AAVSO_OBS_CODE` is set, and held back if
     nothing could be measured.
   - **Anomaly:** an `outburst` is proposed when the source is detected *brighter* than Rubin's light curve predicts. Its
     significance is the brightening divided by a third of the agreement tolerance, `max(0.5 mag, 3 sigma) / 3`, so the band
     mismatch is a systematic floor and a half-magnitude difference counts as 3 sigma, not 30. Fading, disappearing and not
     detected are *not* screened: a fast fader, a moving object, an artefact or a position error explain them as well.
   - An outburst at or above the threshold is flagged in the mission log. The per-alert table (Rubin last, age, expected,
     Seestar, delta, result) and the finder-plus-light-curve figure are on the dashboard. In simulation the rising source is a
     5.3 sigma outburst, so the simulated run flags one.

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_RUBIN_FINK_URL` | `https://api.lsst.fink-portal.org` | Fink endpoint |
| `ASTRONOMO_RUBIN_TAGS` | `extragalactic_lt20mag_candidate,in_tns` | Fink alert filters to pull (comma list) |
| `ASTRONOMO_RUBIN_OBJECT_IDS` | empty | Watchlist of `diaObjectId`s (skip the brightness/age cuts, +priority) |
| `ASTRONOMO_RUBIN_MAX_MAG` | `16.0` | Skip alerts fainter than this (AB mag) |
| `ASTRONOMO_RUBIN_MAX_AGE_D` | `14` | Skip alerts older than this (days); also the stale-stream check |
| `ASTRONOMO_RUBIN_MIN_RELIABILITY` | `0.8` | Minimum real/bogus score |
| `ASTRONOMO_RUBIN_MIN_ALT_DEG` | `30` | Minimum altitude over the next hour |
| `ASTRONOMO_RUBIN_ALERTS_PER_TAG` | `500` | Alerts requested per tag |
| `ASTRONOMO_RUBIN_FRAMES` / `_EXPOSURE_S` | `60`/`10` live; `20`/`10` simulated | Frames and exposure |

**What the numbers mean (be careful):**

- **Reach.** A 50 mm Seestar gets to roughly mag 15-16, but most Rubin alerts are fainter than 19. Expect few live
  targets, and expect "not detected, uninformative" whenever the limit is shallower than the alert.
- **Difference flux.** An alert's `psfFlux` is measured on a *difference image* (`m_AB = 31.4 - 2.5 log10(flux_nJy)`). It
  equals the object's total flux only where the template is empty at that position; for a transient on a bright host the
  Seestar sees host plus transient, so a "brighter than expected" result is not automatically a rebrightening.
- **Band mismatch.** The Seestar is broadband and Rubin's filters are not; the plugin picks the Rubin band closest to the
  camera's among bands with a recent detection, but a few tenths of a magnitude of systematic error remains. That is why the
  agreement tolerance has a 0.5 mag floor.
- **Simplifications.** Translation-only registration, an unclipped mean stack, about 1 e-/ADU assumed, and a Gaia zero point
  without a colour term. The generic dashboard summary's "detections" count shows 0 because results live in the cross-reference entries.
- Alert MJD values are TAI and are converted to UTC. Alert times, not observing times, set the "age".
- Fink's fields and tags were checked against the live API (parsing, tags, light-curve history), but a live end-to-end run
  with a real telescope on a fresh alert has not been done, and the stream was dark when this was written.
