# The asteroid plugin

> **Draft: not ready for use.** This plugin has never run on a real telescope, and its MPC upload needs an observatory code
> that is not being pursued ([why](../notes/mpc-observatory-code.md)). It is kept and tested: it runs in simulation and
> on the ZTF replay, and it proves the core is not tied to one science case. It refuses to command a real telescope unless
> `ASTRONOMO_ALLOW_DRAFT_PLUGINS=1` is set. Its mission logs say "DRAFT".

1. Targets, live, in order of precedence:
   1. `ASTRONOMO_ASTEROID_TARGETS`, if set: your own designation list (labelled `operator`), replacing everything below.
   2. **The Minor Planet Center's NEO Confirmation Page (NEOCP)**: new objects awaiting follow-up astrometry. Entries are
      kept if they have a magnitude, are at most `ASTRONOMO_ASTEROID_MAX_VMAG` (14.5), were last seen at most
      `ASTRONOMO_ASTEROID_NEOCP_MAX_NOT_SEEN_D` days ago (1.5; the feed's position is the *last observation's*, so it
      drifts), have an MPC score of at least `ASTRONOMO_ASTEROID_NEOCP_MIN_SCORE` and are above the minimum altitude.
      Task `astrometry_followup`. **Almost every NEOCP entry is far fainter than a 50 mm telescope can reach (V 19-24)**,
      so on most nights this contributes nothing; `ASTRONOMO_ASTEROID_USE_NEOCP=0` switches it off.
   3. A built-in list of bright well-known asteroids, via JPL Horizons, filtered by altitude and magnitude, **labelled
      `manual`** in the mission log so it is clear that no network list drove the choice.

   Offline = three simulated fields (a known mover, a mover with no designation to check it against, a control).
2. Reduction (`reduction.py`): `Background2D` → `DAOStarFinder` → translation registration from bright stars →
   stationary-source rejection → constant-velocity track linking → aperture photometry → sky motion.
3. Cross-reference: calibrate astrometry against Gaia DR3 (pattern-match translation), then compare each
   track's calibrated position **and rate** with Horizons for the expected object(s).
4. **Outputs.**
   - **Upload file** (the default): an **MPC ADES 2017 PSV** astrometry file per field, from the Gaia-calibrated positions of
     the *detected* frames only (a position predicted for a frame where the object was not found is an extrapolation, not a
     measurement). Each position's uncertainty is the calibration rms combined with the track's scatter, with a 0.1 arcsec
     floor. Magnitudes are left out: the photometry is instrumental. Seestar is described as a 50 mm f/5 refractor with a
     CCD-mode detector.
   - A track is written only if it matched a published designation (`permID` or `provID`), or belongs to a NEOCP target
     (reported under its temporary designation in `trkSub`). A track that matched nothing is **held back**, with a note to
     ask the MPC (MPChecker, the NEO Confirmation Page) whether it is already known. A NEOCP file is a `DRAFT_` with a
     blocker, because the field can contain other movers and the NEOCP position is only good to about a field of view:
     compare the track with the MPC's ephemeris before sending. At least three detected positions are required, and replays
     of survey data are never offered.
   - It is a `DRAFT_` until `ASTRONOMO_MPC_STATION_CODE` (assigned by the MPC) and `ASTRONOMO_OBSERVER_NAME` are set.
   - **Anomalies:** none are proposed yet, so an asteroid mission never flags one in its mission log.
   - The ADES layout follows the 2017 specification; validate with the MPC's tools before sending.

**What the numbers mean (be careful):**

- Sensitivity was measured on *synthetic* frames, one noise realisation per cell: movers faster than
  ~1.5 px/frame were recovered down to roughly mag 15.5 (16.5 for some); slower movers are indistinguishable from stars
  at the 1.2 px stationary tolerance; **0 false positives in 20 star-only null fields** (an ad-hoc check, not
  part of the test suite). That is not a statistical sensitivity study, and real data will be messier
  (field rotation, clouds, satellites, noisy centroids).
- The simulated mover is fast (tens of arcsec/min) so motion shows in ten-second frames. Real
  main-belt asteroids move ~0.5–2 arcsec/min, so live mode spaces frames out (`ASTRONOMO_ASTEROID_INTERVAL_S`,
  default 300 s) to get a few pixels of motion per frame.
- Simulated Gaia/Horizons come from the same synthetic sky as the images, so the sub-arcsecond match
  separations in simulation say nothing about real astrometric accuracy.
- **A track nobody asked a catalogue about is "not checked".** If the target has no designation or watchlist
  (as in the simulated field B), Horizons is never queried, so the track is classified `unverified` ("not
  checked" in the dashboard and the mission log), not "unidentified". The same label is used when there is no
  astrometric solution or Horizons is unavailable.
- **Horizons cannot list unknown objects in a field.** "Unidentified" means "not explained by the objects
  queried", never "new". The upload file says so and points to the MPC. No discovery claim should rest on this
  pipeline without independent confirmation.

## Archive mode: real ZTF images

`--data-source archive` replays Zwicky Transient Facility science images from NASA/IPAC IRSA. MOST (IRSA's
moving-object search) lists the ZTF exposures predicted to contain an asteroid (default `8191`, a main-belt
asteroid with several nights in January-February 2019); the adapter cuts 6' stamps from the IBE image service,
drops frames the ZTF pipeline flagged bad (`INFOBITS`), and gives each stamp an exact-TAN WCS refitted from ZTF's
TPV solution (the fit error is measured and reported). Each *visit* becomes one target with a `not_before`.

| Variable | Default | Meaning |
|---|---|---|
| `ASTRONOMO_ZTF_OBJECTS` | `8191` | Asteroids (comma list: designations or numbers) |
| `ASTRONOMO_ZTF_BEGIN` / `_END` | `2019-01-10` / `2019-02-10` | Search window |
| `ASTRONOMO_ZTF_MAX_VISITS` | `6` | Visits kept per mission |
| `ASTRONOMO_ZTF_MAX_FRAMES` | `12` | Exposures used per visit |
| `ASTRONOMO_ZTF_CUTOUT_ARCMIN` | `6` | Stamp size |

Differences from telescope mode, all deliberate: frames are registered by their WCS (ZTF stamps show too few stars
for star matching); the survey WCS is **kept** and Gaia is only a consistency check (typically ~0.1 arcsec
agreement); the ephemeris comparison uses Horizons for **Palomar** (MPC code I41), not your site, with a 10 arcsec
match radius.

What it has done: run blind on object 8191's visits on 2019-01-20 and 2019-02-09, it found a mover whose position
and rate (about 0.5 arcsec/min) match Horizons; in the second visit it also reports two more tracks. One is a
second real mover; one is marginal and probably spurious. Both are labelled *unidentified*, which here means only
"not the asteroid we asked about": **SkyBoT/MPC are not consulted**, visits are analysed independently (no
cross-night linking), and sensitivity has not been measured on real frames. Treat it as a pipeline demonstration on
real pixels, not a search.

Data: ZTF public data release; the credit line is attached to every mission log (Acknowledgements).
