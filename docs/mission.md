# Mission and goal

What Astronomo is for, what it holds itself to, and how we will know it is working. This page changes a few times a year. The plan that gets there, and what is next, is the [roadmap](../ROADMAP.md). The levels used in the roadmap (Visit, A, B, C, D) are defined in [the concrete goal](#the-concrete-goal-what-astronomo-can-meaningfully-contribute).

## Mission

**Help amateur astronomers contribute meaningfully to real science.**

Astronomo is the free, open, auditable pipeline that makes this contribution ordinary for a Seestar owner. It takes targets from the networks' own lists, observes them under supervision, reduces them honestly, and writes a file the network accepts. A person decides what is sent.

On this telescope the stronger case is variable-star photometry, so it comes first. Transit timing is kept, and has its own later phase ([Phase 6](../ROADMAP.md#phase-6-transits)), because its error bar is harder to make true.

**What we hold ourselves to**

1. **Honest error bars over impressive numbers.** A measurement whose quoted error is true is worth more than a precise-looking one that is not. The pipeline's quoted errors are tested against known answers.
2. **Serve what the networks ask for.** Targets come from their priority lists, and output is in their formats. We do not chase novelty.
3. **A person decides what leaves the system.** Astronomo prepares files and never sends them.
4. **Easy, documented and auditable.** A new owner should reach a first useful night without reading the code, and anyone should be able to check why a target was chosen, how a number was derived and what a file claims.
5. **Open.** Free to use, with a plugin interface so other telescopes and other science cases can join. The Seestar S50 comes first.
6. **No claim beyond the data.** No discovery claims.

**How we will know.** Two ladders, one for our own telescope and one for adoption. They are targets, to be replaced by measured numbers.

|     | Own telescope                                                                          | Adoption                                                                                                       |
| --- | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 1   | **Visit.** A variable-star magnitude within its quoted error ([Phase 2](../ROADMAP.md#phase-2-first-clean-night)) | **R1.** Someone other than the author has run it on a real night, and a file from it was accepted by a network |
| 2   | **B.** One accepted upload file ([Phase 3](../ROADMAP.md#phase-3-first-accepted-file))              | **R2.** About 10 owners have had a file accepted                                                               |
| 3   | **C.** Steady variable-star visits ([Phase 5](../ROADMAP.md#phase-5-unattended-and-automatic), after the honest errors of [Phase 4](../ROADMAP.md#phase-4-claims-you-can-defend)) | **R3.** Hundreds of accepted files a year from outside observers                                               |
| 4   | **A.** One known transit within its error ([Phase 6](../ROADMAP.md#phase-6-transits)), then **D.** One further case ([Phase 8](../ROADMAP.md#phase-8-reach)) | **R4.** The networks mention the pipeline's data as a regular source (their call, not ours)                    |

The adoption numbers are an illustration of scale, not a forecast. Adoption depends on first showing, on one real telescope, that the data are good.

## North star: take part in collaborative Pro-Am science

**Astronomo does not try to discover things a survey will find anyway. It takes part in the networks where amateur data is wanted, does the job those networks have asked for, and hands back a file they can ingest.**

- **Targets come from the networks' own priority lists:** the AAVSO Target Tool (variable-star magnitudes), [ExoClock](https://www.exoclock.space/) and [NASA Exoplanet Watch](https://exoplanets.nasa.gov/exoplanet-watch/) (overdue transit timings), the NASA Exoplanet Archive's TTV flag, the Minor Planet Center's NEO Confirmation Page and public Rubin alerts. Which list chose a target, the task and the reason are recorded on the target and printed in the mission log. A built-in fallback list is always labelled `manual`.
- **The default product is a database-ready upload file:** AAVSO Extended (variable stars), AAVSO Exoplanet Report, or MPC ADES. A person reviews it and sends it. Simulated or replayed data can never be marked ready.
- **A night is packaged.** The product is the upload file plus a short findings summary, a mission log and a recipe for repeating the night, all in one folder a person reviews. The code writes the upload files, a findings summary and a Markdown mission log into one `results/` folder. A fired anomaly (default 5 sigma, enforced in core) is a flag in the mission log for a person to review. The recipe for repeating a night is still to come ([Phase 2.2](../ROADMAP.md#phase-2-first-clean-night)).
- **Variable-star photometry is the stronger case on this telescope** and comes first. The AAVSO Target Tool list is read for the Opportunities page; a visit can be planned and run, and writes one differential magnitude per night on a Gaia comparison ensemble plus an AAVSO Extended file a person reviews. Evidence and risks: [variable-star feasibility](plugins/variable-star-feasibility.md), [the plugin](plugins/variable_stars.md). A window taken from the list is a usable night, not an eclipse, because the list has a period and no time of minimum.

The loop the project aims at has eight stages: propose, set up the telescope, run, collect, ingest and analyse, determine findings, test the findings, submit or rework. A language model is optional, off by default, and never in charge of safety. Where each stage stands today is in [status](status.md#the-loop-stage-by-stage), and the stages map to phases below.

### The concrete goal: what Astronomo can meaningfully contribute

**One S50 will not change a field, and this roadmap does not pretend it will.** What a single small telescope can honestly be is a **reliable contributor of routine, well-calibrated measurements on the targets a network says need them most**, run with so little human effort that a second and third observer could do the same. The larger contribution is **a free, open, auditable pipeline that other S50 owners can run**. That makes documentation, an honest error model and an easy first night part of the goal, not extras.

Three parts, in this order of importance.

**1. Trustworthy data.** An observation is only worth a network's time if its quoted error is true.

- *Magnitudes:* a variable-star magnitude agrees with a published brightness within its quoted error. A bar that is really three times larger is worse than no data.
- *Timings (Phase 6):* over all transit timings, the pull `(measured - published ephemeris) / quoted sigma` has a median magnitude of about 1 or less and an RMS of about 1.
- *Accepted files:* at least 90 % of upload files pass the destination's own validator on the first try (the formats are only spec-checked today).
- *No false alarms:* every anomaly the mission log flags was reviewed by a person and judged real enough to report.

**2. Useful targets.** Observe what the network asked for, not what is convenient. For variable stars, that is stars on the AAVSO Target Tool this site can reach. For transits (Phase 6), at least 75 % of attempted observations are on planets in the top quarter of the staleness ranking, built into short O-C series on a handful of planets rather than scattered points.

**3. A small, steady yield.** These are order-of-magnitude estimates, not measurements, to be replaced after the first real nights. A variable-star yield is not estimated: a visit is short, and the count waits on how many clean nights this site gets.

| Level                     | Target                                                                                                                                                                  | Phase                                  |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| **Visit**                 | One variable-star magnitude on a real S50 that agrees with a published brightness within its quoted error, and the file path works                                      | [2](../ROADMAP.md#phase-2-first-clean-night)        |
| **B. First contribution** | One real upload file accepted by a network's database, reviewed and sent by a person                                                                                    | [3](../ROADMAP.md#phase-3-first-accepted-file)      |
| **C. Steady**             | Variable-star visits as the regular product                                                                                                                             | [5](../ROADMAP.md#phase-5-unattended-and-automatic) |
| **A. Validated transit**  | One known transit whose mid-time agrees with the published ephemeris within its combined error and within about 3 minutes, and the whole path (target, file, validator) works | [6](../ROADMAP.md#phase-6-transits)                 |
| **D. Further case**       | Supernova or nova follow-up, which reuses the variable-star photometry, reaching level B. Other cases: [science options](notes/science-options.md)         | [8](../ROADMAP.md#phase-8-reach)                    |

The honest reading of the transit half: **tens of good timings a year** (about 20 accepted timings in the first 12 months after level A, a stretch of about 40), comparable with a dedicated amateur using a small telescope. A single timing from an S50 is unlikely to detect a transit-timing variation on its own.

**Decision rules (so the goal can fail honestly).**

- If the variable-star magnitudes are systematically wrong and cannot be fixed (frames that are not linear, a floor larger than the quoted error), do not scale that case. Say so in the README.
- If level A shows a systematic timing error above about 5 minutes that cannot be fixed (field rotation, no flat frames, the S50's own timing), do not scale up transits. Say so in the README. The variable-star case does not depend on that result.
- If the pull RMS is clearly above 1 and cannot be explained, quoted timing errors are fixed or the file is not offered, whatever the yield.
- If a network will not take S50 data, the goal for that network is dropped and the file format is kept for others.

**Not goals:** discovery claims, scientific papers, sending anything without a person's review, or competing with survey telescopes on faint targets (NEOCP astrometry, most Rubin alerts).
