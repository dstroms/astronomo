# The first transit mission

Written 2026-11-20, moved out of [the first-clean-night runbook](first-clean-night.md). A transit is not part of the first clean night: it is [Phase 6](../../ROADMAP.md#phase-6-transits). Nothing in Phases 1 to 5 needs one. This page keeps what a transit night needs, so it is ready when the time comes.

## Before spending a night on it

- The clean variable-star night has finished ([Phase 2](../../ROADMAP.md#phase-2-first-clean-night)), so the connection, pointing, focus and reduction are known to work, and a failed check on the long run is not the first time they have been seen.
- The Opportunities page's timing error is still an assumption (`ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN`, default 3 minutes). This night is what replaces it with a measured one.

## The mission

A transit is about 4 hours (480 frames of 10 s, with 20 s between them) and has to include the mid-time and time on either side of it. That is far longer than the 20-minute variable-star visit, and an alt-az mount turns the field the whole time.

**Plan it.** On the Opportunities page, switch the kind of observation to transits, turn on **First-light target**, and save a plan for one of those. The page labels it a verification target: bright, deep, high, and already well known. That is [level A](../mission.md#the-concrete-goal-what-astronomo-can-meaningfully-contribute) of the concrete goal. Details of the light curve are in [the transit plugin](../plugins/transit.md).

**Run it.** The same commands as the clean variable-star night (see [the first-clean-night runbook](first-clean-night.md#the-commands)), with the transit plan's own id, without `ASTRONOMO_VARSTAR_FRAMES`, on the home network with `ASTRONOMO_ASSUME_CLEAR` unset. Stay until it parks. A failed focus or pointing check stops the mission before the long run.

## What it records

- [ ] The visit finishes on the telescope and covers the mid-time
- [ ] Its plan moves from PLANNED to STARTED to DONE
- [ ] The mid-time agrees with the published ephemeris, within the combined error and within about 3 minutes
- [ ] That timing error is written down (row 14 of the [open hardware questions](../hardware-questions.md)), in place of the assumed 3 minutes
- [ ] An AAVSO Exoplanet report is written from these frames
- [ ] The report attaches the first frame with the target (T) and the comparison stars (C) marked
- [ ] Field rotation across the long series is measured (row 11), and the light curve is kept or refused on that result

The decision rules that apply, if the night does not agree, are under [the concrete goal](../mission.md#the-concrete-goal-what-astronomo-can-meaningfully-contribute).
