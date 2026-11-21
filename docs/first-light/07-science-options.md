# 07. What else can a Seestar S50 Pro and Astronomo do, and how likely is it to work?

Written 2026-10-29. This ranks every kind of science considered so far by how likely it is to produce a contribution a
network accepts, how much that contribution would be worth, and how much of it Astronomo already covers. The variable-star case is in [the feasibility note](../plugins/variable-star-feasibility.md).

**Every percentage here is my judgement, not a measurement.** They assume an S50 Pro is bought, and a few months of clear
nights. Nothing has been run on an S50 Pro. Much of the evidence is from owners of the older S50, and several sources
are single blog posts. Treat the numbers as a way to compare options, and re-set them after the first real nights.

* **Accepted** means a data set or file that the destination network takes.
* **Useful** means it fills a gap a network asked for, or is used in an analysis.

## The table

| Science | Accepted | Useful | Evidence | Does Astronomo cover it? |
|---|---|---|---|---|
| **Exoplanet transits** | about 40 % | about 15 to 25 % | A NASA Exoplanet Watch participant uses an S50 for transits. A 1 to 2 % dip is about the size of the noise on a 10-minute stack at V 11 to 12. Multi-hour tracking on an alt-az mount is the weak point | Pipeline built, unproven on a telescope |
| **Variable stars, slow classes** (Mira, semi-regular, young stars, outbursts) | about 80 to 85 % | about 70 % | The AAVSO has S50 example pages, the `TG` filter code and VPhot support. Signals are 25 to several hundred times the noise on a 10-minute stack | A simulated visit writes one `TG` magnitude and an AAVSO Extended file (2026-11-14). Comparison stars are a Gaia ensemble, not a VSP chart. Not run on the telescope |
| **Variable stars, eclipse timing** | about 40 to 55 % | about 30 % | Needs a clock good to under a minute, which is unconfirmed (the S50's frame times come from the phone) | A minimum inside a series is timed in simulation. A listed window is still a night, because the Target Tool has no time of minimum. Not run on the telescope |
| **Bright supernovae and novae** | about 75 to 85 % per event | about 40 % | The Transient Name Server's astronotes 2026-105 and 2026-214 list S50 data inside the BHTOM network's follow-up. One blog has an S50 Pro agreeing with an S50 to 0.07 mag on a V 13 supernova | Not built, but it shares the pipeline with variable stars |
| **Comet magnitudes** (when a bright one appears) | about 60 % | about 15 % | One S50 comet report to the MPC through Tycho Tracker. Visual observers already cover bright comets | Not built |
| **Asteroid rotation light curves** | about 50 to 60 % | about 20 % | No S50 example found. The British Astronomical Association says there is no single repository for light-curve data | Not built |
| **Asteroid occultations** | about 25 % | high per event, but rare | An IOTA-ES report found the S50 usable but its raw video has no reliable timestamps, so you need an extra GPS-flash timing rig. It was tested on an S50, not a Pro. Roughly 3 events a week within 1000 km at the report's conservative filter (Berlin) | Almost none: a different pipeline and extra hardware |
| **Asteroid astrometry to the MPC** | about 5 to 10 % | negligible | An observatory code needs measurements of 10 numbered near-Earth asteroids fainter than mag 16, each on two nights. A 50 mm telescope probably cannot do that | Built (`asteroid_finder`), but the code requirement blocks it |
| **Rubin alert follow-up** | about 10 % | negligible | Almost every alert is far fainter than a 50 mm telescope reaches | Built, rarely usable |

## How valuable are the contributions?

Each contribution is a data point in a shared archive, not a discovery. Where the value actually sits:

* **Gap filling.** The AAVSO Target Tool lists stars that have not been observed for a year or more against the request on
  file. A repeating observation of those is real, modest value.
* **Speed.** BHTOM's own note says an S50 covered the rising phase of SN 2026sqf from the night of discovery. A cheap
  robot that responds within hours, when larger telescopes are clouded out, is where Astronomo adds the most.
* **Credit.** NASA's Exoplanet Watch says your name goes on a paper if your observations are used in it.
* **Limits.** BHTOM's note on SN 2026dix says the lack of host-subtracted photometry and the mix of instruments limit
  precise interpretation. Transit timing from a V 12 star at 50 mm is weak compared with the 5-inch-class telescopes
  ExoClock asks for.

## What this suggests building

One photometry pipeline covers three of these: green-channel extraction, a plate solve per frame, aperture photometry
against comparison stars, and an AAVSO writer. The variable-star half of that pipeline now runs in simulation
([the plugin](../plugins/variable_stars.md), 2026-11-14). Supernova and nova follow-up would reuse it and are not a
plugin yet. Transits already have their own pipeline. Occultations and asteroid astrometry would each need separate
hardware or paperwork. What is still unproven about variable stars is in [the feasibility note](../plugins/variable-star-feasibility.md).

## Sources

* NASA Exoplanet Watch community page (a Watcher using a ZWO S50 for transits):
  `https://science.nasa.gov/citizen-science/exoplanet-watch/community/`.
* NASA Exoplanet Watch, how to submit data (AAVSO upload, co-authorship):
  `https://science.nasa.gov/citizen-science/exoplanet-watch/how-to-contribute/how-to-submit-your-data/`.
* Transient Name Server astronotes (BHTOM follow-up that includes the S50):
  `https://www.wis-tns.org/astronotes/astronote/2026-105` and `https://www.wis-tns.org/astronotes/astronote/2026-214`.
* Chris Tse, "Measuring Supernova SN 2026aaiv with Two Seestars" (S50 and S50 Pro; one blog):
  `https://chris.tsehome.com/sn-2026aaiv-measured-with-two-seestars/`.
* Seestar S50 comet report to the MPC through Tycho Tracker (one forum post, Cloudy Nights):
  `https://www.cloudynights.com/forums/topic/896480-seestar-s50-and-citizen-science/`.
* British Astronomical Association Asteroids and Remote Planets Section, "Publication of observations" (no single
  repository for asteroid light curves): `https://britastro.org/asteroids/Publication%20of%20observations.htm`.
* IOTA-ES, "Seestar S50 for Occultation Work" (timing and the GPS-flash requirement; tested on an S50):
  `https://www.iota-es.de/seestar_report.pdf`.
* Minor Planet Center, "Guide to Minor Body Astrometry" (the observatory-code requirements):
  `https://www.minorplanetcenter.net/iau/info/Astrometry.html`. See also [04](04-mpc-observatory-code.md).


---

Moved here from the roadmap on 2026-11-20, text unchanged apart from the links. **Phase numbers in this appendix are the old ones** (the mapping is at the top of [decisions](../decisions.md)). Where this appendix and the table above disagree, the table above was written first and the appendix is newer.

## Appendix: candidate science cases for a 50 mm telescope

Where else a Seestar S50 (50 mm f/5, about 2.4 arcsec/pixel, a 1.3 x 0.7 degree field, roughly mag 15-16 on a stack, colour sensor, alt-az mount) could add something to a collaborative network. Variable-star photometry is already a plugin, runs in simulation (2026-11-14), and is the stronger case on this telescope. The other rows are **candidates, not commitments**, and they do not have a plugin. **Each network's current rules for accepting Seestar data have not been checked.** Check them first. Each one is the same shape as the existing plugins: targets from a network, a reduction, an upload file, and an anomaly screen for what to flag in the mission log.

### Strongest candidates (in the order to try them)

1. **Variable-star photometry (AAVSO).** Amateurs are the main long-term monitors here, and the targets are bright.
  - Eclipsing-binary minima timing (period changes are a real signal), pulsators timed at maximum (RR Lyrae, delta Scuti, Cepheids), long-period variables (Miras, semi-regulars), and outburst monitoring of dwarf novae and other cataclysmic variables (the AAVSO issues alert notices).
  - Fit to the code: built 2026-11-14 as `plugins/variable_stars/`, reusing the transit differential photometry. One point per night, a Gaia comparison ensemble, the green channel (`TG`), and an AAVSO Extended file. A simulated night recovers an injected minimum and both magnitudes. Comparison charts from the AAVSO's VSP service are not used, and a listed window is still a night because the Target Tool has no time of minimum. On 2026-11-18 two Hat-P-23 visits on the telescope measured a Gaia G magnitude; the live AAVSO file was held because the telescope Wi-Fi has no internet. See [docs/plugins/variable_stars.md](../plugins/variable_stars.md).
  - Anomaly candidates, proposed in the plugin: an outburst brighter than the catalogued maximum, an eclipse minimum off its predicted time, a Mira far from a predicted maximum. As with transits, quoted errors will miss correlated noise, so a flag is a lead. A star that was not confirmed against Gaia scores 0.
2. **Asteroid light curves.** A better use of the asteroid plugin than astrometry (bright known asteroids already have excellent orbits).
  - Rotation periods for bright asteroids go into public databases (ALCDEF, the Minor Planet Bulletin, the Collaborative Asteroid Lightcurve Link), and many bright objects still lack a good one. It needs several nights per object and about 0.02 mag precision, plausible at V < 14, and the wide field supplies comparison stars.
  - New: multi-night planning per object (depends on the Phase 5 scheduler), period fitting with an honest alias check, and an ALCDEF-format writer.
3. **Comets.** Brightness reports (COBS) and astrometry for comets with poorly known orbits. A few reach mag 8-13 each year, and comet orbits are less certain than bright asteroids', so positions are worth more. New: comet ephemerides and magnitudes (Horizons covers them; not yet used here), coma-aware photometry, and an ADES comet record plus a COBS-style brightness report.

Considered and ranked lower: Galactic nova and bright-supernova patrol (a small field against all-sky patrols, and it needs image subtraction), blazar monitoring when bright, and faint-nebula hunting (the artifacts, flat-field and novelty problems are a different project from everything above). Not feasible with this telescope: occultation timing (needs fast, accurately timed video), faint-target work (NEOCP, most TESS follow-up, microlensing) and planetary imaging.

### What would need to change

- **Colour handling.** AAVSO-style photometry wants the green channel (`TG`). `tools/imaging.py` now extracts it from a Bayer mosaic or a 3-colour cube and keeps the pixel grid, so the world-coordinate solution still applies (2026-11-14). The zero point is Gaia G, not a transformed Johnson V, and the file says so. What the S50 actually writes to its FITS share was checked on 2026-11-16: the header has no `BAYERPAT`, so this extraction does not run on real frames. Sharp stars are averaged 2x2 instead (`mosaic_averaging`), and the true pattern is still unconfirmed.
- **Calibration.** There are no dark or flat frames. Differential photometry cancels some of the error, but it must be measured (injection tests on real frames) and quoted as a systematic, not assumed small. This is already a Phase 2 item (honest photometry); these cases depend on it.
- **Field rotation.** The mount is alt-az. Registration corrects a rotation about the frame centre. The Hat-P-23 visits rotated 1.64° and 0.84° and the reduction corrected both. A multi-hour transit is still the case that can throw the target off the comparison stars, and the limits are still guesses (Phase 1).
- **Targets.** The AAVSO Target Tool list is read (Opportunities page, `plugins/variable_stars/`), and a window from it can be planned and reduced (simulation, 2026-11-14). The AAVSO's VSX and VSP services give each star's period, epoch and comparison stars (checked 2026-10-29 on `vsx.aavso.org` and `apps.aavso.org`; the `www.aavso.org` host refuses scripts). Those two services are not called yet, so an eclipsing-binary plan is a usable night and the comparison stars are a Gaia ensemble. See [docs/plugins/variable_stars.md](../plugins/variable_stars.md) and [docs/plugins/variable-star-feasibility.md](../plugins/variable-star-feasibility.md).
- **Scheduling across nights.** Light curves and long-period variables need repeat visits. Depends on the Phase 5 scheduler and multi-day missions; a goal over many nights is a campaign (Phase 5.3).
- **Honest claims.** A new anomaly kind needs an honest significance, a systematic floor and an "identity unconfirmed scores 0" rule, as for transits and Rubin. Nothing is promoted past a lead without independent confirmation.
- **A known-answer test first** for each case: a star with a published light curve or period, an asteroid with a published rotation period, a comet with reported magnitudes. The case is not worth building if the S50 cannot reproduce the known answer. Variable stars have that test in simulation (an injected eclipsing binary and a steady control). The same comparison on the telescope is still open.

Exit criterion for starting any of these: the first clean night (Phase 2) has measured the S50's actual photometric precision and field-rotation behaviour, so the table above can be replaced with measured numbers. Variable-star photometry was built in simulation before that measurement (2026-11-14). A short visit on the telescope has reproduced Gaia G (Hat-P-23, 2026-11-18). The case stays open until a clean 40-frame night does, with a forecast the mission fetched and an AAVSO file written on that run.
