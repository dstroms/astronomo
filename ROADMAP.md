# Roadmap

The plan: what each phase delivers, how it is built, and when it is done. **A phase is done when its exit criteria are met, not when every idea in it is explored.** Open questions are parked in the phase that needs the answer. Nothing in a later phase blocks an earlier one.

**Where things live.** This file is the plan, and the only place that says what is next and how each step is built. The mission and the goal: [mission](docs/mission.md). What is real and what is not: [status](docs/status.md). What happened on each night: [field journal](docs/journal.md). Why it is built this way: [decisions](docs/decisions.md). Every document in the repository is listed in the [documentation map](#documentation-map), and a test (`tests/test_docs_linked.py`) fails if one is missing or a link is broken.

## Now

*Last updated 2026-11-21. This block is the starting point when you come back. Update it first, and keep the detail in the phases below and in the journal.*

**Where we are.** Two short (12-frame) variable-star visits finished on a real Seestar S50 Pro on 2026-11-18 (Hat-P-23). The reanalysed magnitudes, 12.145 ± 0.057 and 12.139 ± 0.042, are inside the quoted error of Gaia G 12.162. Pointing correction, autofocus and a local plate solve have worked on the sky. On 2026-11-20 the telescope joined the home network in station mode, after the router was moved off `10.0.0.x`, and one computer reached the telescope, Open-Meteo and the Harvard VizieR mirror together. The hardware check passed. Not yet seen: a whole mission over station mode, a live AAVSO file, a transit that covers a mid-time.

**Next, in this order.**

1. [Phase 2, first clean night](#phase-2-first-clean-night): keep the telescope awake while connected (2.1), build the recipe for repeating a night (2.2), then a 40-frame variable-star visit on the home network with nothing assumed (2.3).
2. [Phase 3, first accepted file](#phase-3-first-accepted-file): retry buttons (3.1), then finding out what the AAVSO wants to receive, and meeting it with a real file (3.2).

Transits are [Phase 6](#phase-6-transits). Nothing before it needs one.

## Mission

**Help amateur astronomers contribute meaningfully to real science.** The principles, the two "how we will know" ladders (our own telescope and adoption) and the north-star goal are in [mission](docs/mission.md). The levels named below (Visit, A, B, C, D) are defined in [the concrete goal](docs/mission.md#the-concrete-goal-what-astronomo-can-meaningfully-contribute).

## Phases

Eight phases. The order is the path: **a public repository, then a clean night, then a file a network accepts, then claims you can defend, then unattended and automatic running, and only then transits.** Phase 1 is done and is kept under [Completed phases](#completed-phases). Phases 2 and 3 are in sequence. Phases 7 and 8 are later and not yet scheduled.

**How the phases cover the goal's loop** (propose, observe, analyze, test, then submit or rework; the [mission](docs/mission.md) page and the README start from it):


| Step                    | Delivered by                                                                                                                                         |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Propose what to observe | [5.4](#phase-54-plan-the-night-without-a-person) rules in code that a person approves, then [7.1](#phase-7-autonomy) a model proposing within bounds |
| Observe                 | [Phase 2](#phase-2-first-clean-night) a clean night, [Phase 5](#phase-5-unattended-and-automatic) unattended and over many nights                    |
| Analyze                 | [4.1](#phase-4-claims-you-can-defend) honest errors, then [7.1](#phase-7-autonomy) a model accepting or re-observing                                 |
| Test the finding        | [4.2](#phase-4-claims-you-can-defend) claim levels, then [7.2](#phase-7-autonomy) confirmation observations                                          |
| Submit                  | [Phase 3](#phase-3-first-accepted-file) a file the AAVSO accepts, then [8.2](#phase-8-reach) sending after a recorded sign-off                       |
| Rework                  | [7.2](#phase-7-autonomy) follow up, redesign or drop, within hard limits                                                                             |


**How to read a phase.** A top-level phase (Phase 3) has a goal, what is already done, its sub-phases (Phase 3.1, 3.2 and so on) and **exit criteria**: the phase is finished when all of its exit criteria are true, and not before. A sub-phase is one piece of work: a checklist of tasks and a *done when*. Tick a task when it is done, and a phase when its exit criteria are. A finished phase moves to [Completed phases](#completed-phases) at the end, cut down to a short summary and its exit criteria, and its detail goes to the journal. Where a task from the old phase lists went is in the [field journal](docs/journal.md#the-old-phase-lists-phases-1-to-6-as-of-2026-11-20).

**Standing rule, not a task.** The README's status block and limitations say what has been seen on a real telescope and what has not. Keep its phase list in step with this file. It has no checkboxes or dates on purpose, so it should rarely change. Re-read it against this file whenever a phase closes.

### Phase 2: First clean night

**Goal:** one supervised variable-star visit that finishes with nothing assumed, and leaves a recipe that someone else could repeat. The order and commands are in [the first-clean-night runbook](docs/runbooks/first-clean-night.md).

**Done:** connect and hardware check (2026-11-13, again over station mode 2026-11-20); the pointing check, a local plate solve and a corrected slew (2026-11-18, twice); autofocus (2026-11-16); the first-frame view and per-frame watch; a colour term; the 70 degree altitude cap; two 12-frame visits that finished and matched Gaia G (2026-11-18). On 2026-11-20 the telescope was reached over the home network, with the internet reachable at the same time (bench only). Detail: [journal](docs/journal.md).

#### Phase 2.1: Keep the telescope awake while connected

**Why:** a clean night must not end because the scope went to sleep or dropped the connection while the computer was busy or waiting (a weather wait, a slow reduction call, a pause between steps). Today the controller sends a status ping every 8 seconds during long waits inside a mission (`HEARTBEAT_S` in `tools/telescope.py`, marked there as unconfirmed that it is needed). It is a fixed constant, not a setting, and nothing says what the S50 itself does when it is left idle (its own auto-off or sleep timer is not recorded in the [hardware questions](docs/hardware-questions.md)).

- [ ] **Find out what the S50 does when idle,** over Alpaca and station mode: how long until it sleeps, powers down or drops the connection, with the heartbeat off and on. Write the answer into the hardware questions.
- [ ] **Keep it awake for as long as Astronomo is connected,** including waits that today have no ping (between plan start and slew, a weather wait while still connected, a slow outside call during reduction).
- [ ] **An option to turn it off** (a setting, on by default, for example `ASTRONOMO_SEESTAR_KEEP_AWAKE`), for an owner who wants the scope to sleep or who has the app's own timer set. It is listed in [the settings page](docs/configuration.md), and the mission log says which way it was set.
- [ ] **Decide what a long weather wait does.** Keep the scope connected and awake, or park and release it, and wake it later. Park-and-release is safer for the mount and the battery.

*Done when:* in simulation a test shows the ping runs during every kind of wait and stops when the setting is off. On the real scope, a 30-minute idle connection stays alive with the setting on, and what happens with it off is written down.

#### Phase 2.2: A recipe for repeating the observation

**Is this the same as the mission log?** No, though they share a source. The mission log is the account of one night after the fact: what the telescope did and what came of it. A recipe is the opposite direction: everything another person needs to make the same observation, and to know whether they got the same thing. Today the mission record holds much of this but nothing gathers it, and nothing can be turned back into a plan. The recipe is a section of the findings package (1.6) next to the log, and a file a program can read.

- [ ] **What the recipe holds:** the target (name, coordinates, which network list chose it and why), the site, the observing window and the conditions that were accepted, the equipment (telescope model, firmware version, camera settings), the exposure, gain, frame count and cadence, the focus and pointing steps taken and what they measured, the comparison stars, the reduction settings, and the Astronomo version, the settings that were in force (which `Settings.public_dict()` already gathers) and identifiers for the input data (catalogue, release and date).
- [ ] **A human-readable page and a machine-readable file** (`recipe.json`). The page also says which manual steps a person does on the telescope, linking [the runbook](docs/runbooks/first-clean-night.md) rather than copying it.
- [ ] `**python main.py --plan-from-recipe FILE`** creates a plan with the same settings, so a second person (or the same one, next season) repeats the night.
- [ ] This also covers the reproducibility record that was in [Phase 4.2](#phase-4-claims-you-can-defend): code version, config and input data identifiers are stored with each result.

*Done when:* in simulation, a mission's recipe loads into a plan whose settings match the original, a test covers it, and the real clean-night mission has a recipe that was read by someone who did not run it.

#### Phase 2.3: Run the clean visit

- [ ] On the home network, with `ASTRONOMO_ASSUME_CLEAR` unset: 40 frames, a target under about 70 degrees, a forecast the mission fetched itself, the VizieR mirror reached during the run, an AAVSO Extended draft written on that run, and the telescope parked. It is the first time a whole mission runs over station mode. Also unseen: whether the address and the Alpaca switch survive a power cycle. If `seestar.local` stops resolving, look the address up again.

*Done when:* the visit finished with nothing assumed, and the magnitude agrees with the published value within its quoted error, or the disagreement is explained.

#### Phase 2.4: Answer the open hardware questions

- [ ] **Fill the rows this night answers** ([open hardware questions](docs/hardware-questions.md)): the Bayer pattern (row 19), the exposure-start time against a clock (row 20), the longitude sign (row 21), the picture compared with the Seestar app's (row 18), Sun, Moon, altitude and the mount's slew limit against a planetarium (row 15), the free-space and dew checks letting a good night continue, and the dashboard seen showing the first frame. Alpaca selects no filter and has no plate-solve call, so those rows stay open by design. Close the firmware decision box when the Alpaca rows are filled.
- [ ] **See the two cases not yet seen:** a first slew that is already on the chip, and an unsolved frame that stops the night.
- [ ] **See a transient fault on a real run,** a dropped network or telescope link, and check that the mission retries and carries on instead of failing on the first occurrence. The retries are built, and have only met simulated faults. If no fault happens on its own, write down that it was not seen.

*Done when:* every row this night can answer is answered, and the two cases and a transient fault have been seen or are written down as not seen.

#### Phase 2.5: Settle the guessed settings

- [ ] **Decide the two options that are off by default,** with data from this night: a deliberately soft focus (`ASTRONOMO_FOCUS_TARGET_FWHM_PX`, try 3.5 then 0) and a recentring slew (`ASTRONOMO_POINTING_RECENTRE_ARCMIN`, only if an off-centre start loses the target from the chip).
- [ ] **Find out whether the slow trend recurs.** The target gained about 0.12 to 0.15 mag on its comparison stars across a sequence on two nights. Flat frames did not fix it, and the attempt was removed.
- [ ] **Re-set the guessed thresholds from real frames:** focus width, pointing, and the rotation limits (`warn_rotation_edge_px` 1.5, `max_rotation_edge_px` 4).

*Done when:* each option and threshold is either re-set from data or labelled as still a guess in the README.

**Exit criteria (all required):**

- [ ] A 40-frame variable-star visit has finished on a physical Seestar, supervised, with nothing assumed, on a star with a published magnitude, and the magnitude agrees within its quoted error or the disagreement is explained (2.3).
- [ ] The driver, frame retrieval and parking are confirmed against real firmware, and the README no longer says "untested" for them. Pointing and focus are verified on the run, and a failed check stops the mission with a clear message (2.3, 2.4).
- [ ] Weather, Sun, Moon, window and altitude checks have been seen working on the real mount, not only in simulation (2.4).
- [ ] A transient network or telescope error does not kill a mission on its first occurrence (built; unproven on a real fault) (2.4).
- [ ] The telescope stays awake for the whole visit with keep-awake on, and it can be turned off (2.1).
- [ ] The clean night's mission has a recipe that loads into a plan with the same settings, and someone who did not run the night has read it (2.2).
- [ ] The README's limitations are updated with what the run showed (2.5).

### Phase 3: First accepted file

**Goal:** we know what the AAVSO wants to receive, and a real file from a real night in that form is accepted by them (level B), reviewed and sent by a person, and a failed outside call never leaves a night stranded. The AAVSO has not yet released a standardized format for its new intake, so the first job is to find out, not to assume the Extended file is what they want. The findings package it is sent from is built in [Phase 1.6](docs/journal.md#phase-1-public-release-as-completed-2026-11-21) and the recipe in [Phase 2.2](#phase-2-first-clean-night).

**Done:** the AAVSO observer code, site and Seestar equipment are in the account (2026-11-14). The writer emits AAVSO Extended (`TG` filter code) and a finder image with the target and comparison stars marked. A missing code keeps a file a `DRAFT_`, and simulated files stay `SIMULATED_`.

#### Phase 3.1: Retry buttons

A mission must not sit in limbo because an outside service failed. Today a call to a third party that fails mid-mission can leave a mission half-finished while the data to finish it is already on disk. On 2026-11-18 plan #22 ended that way: the 12 frames were saved, the live reduction could not resolve `vizier.cfa.harvard.edu`, the AAVSO file was `held`, and the magnitude appeared only after `python main.py --reanalyze` was run by hand on another network. The goal is that a failed outside call is a visible, recoverable state, and that a mission can be analysed whenever the data can eventually be fetched. Nothing here changes what is sent: a person still decides, and a retry never sends a held file.

- [ ] *Name the state.* Record on the mission which outside call failed (the service, what it was for, when, the error), and show it as its own state ("waiting on Gaia", "waiting on the forecast") instead of a generic hold. It is saved with the mission, so it survives a restart.
- [ ] *A Retry button on the mission page, and `python main.py --retry MISSION_ID` for a terminal.* It repeats only the failed step, from the saved frames and data. It never touches the telescope and never re-takes frames, and it is not offered for a step that needs the mount. It builds on `--reanalyze`, the node retries (`ASTRONOMO_NODE_RETRIES`) and the `held` manifest status.
- [ ] *A retry that works finishes the same mission.* Its results and files update in place, the file takes the status its normal rules give it (for example `held` to `draft`), and the log says it was a retry. A retry that fails again keeps the state and the new error. A person presses the button; nothing retries forever on its own.

*Done when:* in simulation, a mission whose Gaia, VizieR or Open-Meteo call fails ends in the named waiting state, the Retry button completes it once the service answers, and a test covers both. On a real night, switch the computer's network off during the reduction on a rehearsal, switch it back on, press Retry, and write down what happened.

#### Phase 3.2: Find out what the AAVSO wants, then meet it

The AAVSO Extended file is the format Astronomo writes today, because it is the only one that is published and stable. It may not be what the AAVSO wants in the end. As of 2026-11 the AAVSO appears to be building its own intake tool or channels, and we have not found a standardized format for them that has been released. Until it exists, "a file the AAVSO accepts" is not something we can define or test against, so this step starts with finding out what the AAVSO wants, and does not start by uploading.

- [ ] **Write down what is known and what is not.** Read the AAVSO's current submission pages, forum and announcements. Record in `docs/notes/` which intake routes exist today (the WebObs upload of an Extended file, the Exoplanet report, anything newer), which are said to be going away or changing, what the new tool or channels are said to be, and whether a format or an API has been announced. Each statement carries its source and the date it was read. Nothing is written as fact that was not read.
- [ ] **Ask the AAVSO.** Contact their staff (the observer support and technical contacts they publish), and ask: what should a new software tool send, which format and version, is there a validator or a test or sandbox intake, is there an API, who reviews a first submission, and what do they say about an automated source with a person approving each file. Say plainly that the telescope is a Seestar S50 with a 50 mm aperture, what the tool does, and that a person reviews every file. Record the answer, or the lack of one, in the journal.
- [ ] **Decide the target format from the answer.** Keep writing Extended while it is the only accepted route, and keep the writer behind one small interface so a new format can be added without changing the mission. If a new format or API is announced, write the new writer against its spec. If nothing is announced, say so here and keep Extended.
- [ ] **Check the file against whatever check exists.** That means the AAVSO's own validator or test intake if they give one. If none exists, check against the published Extended specification and say so, because that is not the same as the AAVSO accepting it. The finder image has not met a real Seestar frame.
- [ ] **A person reviews the package, uploads and records the acceptance.** Astronomo cannot know what a network accepted, and does not guess. If the intake is in flux, a person uploads by hand through the route the AAVSO names, and the response is written down.

*Done when:* the AAVSO's current position is written down with sources and dates, a question has been put to them and the answer (or the silence) recorded, and a target format is chosen. Then, one real file is accepted by the route they name, reviewed and sent by a person, and the AAVSO's response (accepted, rejected or warned, and why) is written in the journal. If the AAVSO has no usable route yet, the first part is the finish: say so here, with the date, and recheck.

#### Phase 3.3: Decide on the AAVSO's comparison-star service

- [ ] Comparison stars are a Gaia ensemble today, not a VSP chart, and a window from the Target Tool is a night, not an eclipse. Calling VSX (period, epoch) and VSP (comparison stars) would fix both. Decide from what the AAVSO says it wants in 3.2, including whether their new tool supplies comparison stars itself.

*Done when:* a decision is written here, with the reason.

**Exit criteria (all required):**

- [ ] What the AAVSO accepts today, and what it has said about its new intake, is written down with sources and dates, and the AAVSO has been asked directly (3.2).
- [ ] One real file, in the format the AAVSO names, is accepted by them, reviewed and sent by a person (level B), and the acceptance is written in the journal (3.2). If they have no usable route yet, this is replaced by a dated note saying so and a date to recheck.
- [ ] In testing, a mission with a failing outside call ends in a named waiting state and the Retry button finishes it (3.1).
- [ ] The file's own failure modes are known: what the AAVSO rejected or warned about, written down (3.2).

### Phase 4: Claims you can defend

**Goal:** every result says how sure it is and how it could be wrong, and a claim is stated no more strongly than the evidence allows. Still no language-model changes. This comes before unattended running ([Phase 5](#phase-5-unattended-and-automatic)): a visit nobody watches should already state its error.

#### Phase 4.1: Honest photometry

- [ ] Report aperture dependence as a systematic error, use mid-exposure times, and calibrate the e⁻/ADU assumption for the Seestar.

*Done when:* results carry a quoted systematic error rather than a bare number.

#### Phase 4.2: Science-claim verification

A result may only be stated as strongly as the evidence allows, enforced in code, so neither a template nor the model can overstate it.

- [ ] *Claim levels* with explicit criteria (`detected`, `consistent with known object`, `unidentified candidate`, `independently confirmed`), shown in the mission log, upload-file notes, the findings summary and the dashboard. Nothing is promoted to `independently confirmed` automatically.
- [ ] *Known-answer regression tests* in CI: the ZTF archive replay and a variable-star known answer become committed tests with expected values and tolerances. Transit cases are in [Phase 6](#phase-6-transits).
- [ ] *Injection-recovery and null fields on real frames,* to measure sensitivity and the false-positive rate. Today this exists only on synthetic frames.
- [ ] *Human review gate:* nothing leaves the system without a person signing off on the claim level. The reproducibility record (code version, config, input data) is built in [Phase 2.2](#phase-2-first-clean-night).

*Done when:* every upload file and findings summary states a claim level computed in code from the evidence, known-answer regression tests run in CI, and a measured false-positive rate and sensitivity on real frames are quoted wherever a candidate is reported.

**Exit criteria (all required):**

- [ ] Results carry a quoted systematic error rather than a bare number (4.1).
- [ ] Every upload file and findings summary states a claim level, computed in code from the evidence, and known-answer tests run in CI (4.2).
- [ ] A measured false-positive rate and sensitivity on real frames are quoted wherever a candidate is reported (4.2).

### Phase 5: Unattended and automatic

**Goal:** visits run without someone watching, and the nights are planned without a person making each plan (level C: steady variable-star visits as the regular product). Still no language-model changes. A person still approves what runs, and the interlocks stay in code.

**Done (built and tested in simulation; not seen on the mount):** `python main.py --schedule` lists due plans and resumes a weather wait, and a plan starts only with `--confirm ID` (self-start is off and refused unless `ASTRONOMO_UNATTENDED_CONFIRMED` is set); the plan life cycle and the hand-off from a plan (`--from-plan ID`); rescheduling on weather; free-space, dew and battery checks; the site profile and interlocks; the React dashboard over FastAPI with the Opportunities view, which lists network targets with reachability, a window and a labelled impact estimate, and saves a window as a plan.

#### Phase 5.1: Self-start and multi-day missions

- [ ] **Self-start, only after the owner confirms the mount.** `ASTRONOMO_UNATTENDED_CONFIRMED` is the record that the interlocks and parking have been seen on this mount. Nothing in the repository sets it.
- [ ] **Multi-day missions.** Depends on the scheduler. Watch checkpoint growth (`observations` accumulates in state) and move bulky data out of state if it gets large. This is one mission that survives days. A goal pursued over many nights, with the nights combined, is a campaign ([Phase 5.3](#phase-53-campaigns-one-goal-over-many-nights)).

*Done when:* a multi-night mission starts, sleeps through bad weather, resumes and finishes with nobody touching it.

#### Phase 5.2: Safety for unattended use

- [ ] A window that ends mid-sequence (the window is checked when a target starts), and an automatic obstruction mask (a person can draw a horizon by hand). Retries have only met simulated faults.

*Done when:* a run whose window closes mid-sequence stops cleanly and parks, seen on the mount or in a simulation that models it.

#### Phase 5.3: Campaigns: one goal over many nights

Today a mission is one run: one night, one or a few targets. A **campaign** (a meta mission) is one goal pursued over many nights, for example 15 nights on one variable star, with the nights observed to the same recipe, combined, and packaged together. It builds on the scheduler (5.1), the recipe ([Phase 2.2](#phase-2-first-clean-night)), the findings package ([Phase 1.6](docs/journal.md#phase-1-public-release-as-completed-2026-11-21)) and quoted systematic errors (4.1).

- [ ] **A campaign record.** The target or targets, the goal and its stop rule (a number of nights or visits, coverage of the star's cycle, or an end date), the cadence wanted (for example one visit a night) and the windows allowed. A campaign owns one ordinary mission per night. Each night's mission keeps its own full record and is never rewritten by the campaign.
- [ ] **Hold the setup fixed, and say when it is not.** The same channel, comparison stars, exposure and reduction settings on every night, taken from the recipe. A night observed with different settings is flagged and left out of the combination until a person says otherwise. It is never merged silently.
- [ ] **Combine the nights.** One light curve across nights with every visit kept, and a per-night check that the comparison stars agree from night to night. A night that disagrees is flagged. Anything derived from the combination (a period, a time of minimum) carries the systematic error from 4.1. Nothing is averaged away.
- [ ] **Package the campaign.** One folder: an upload file with every visit as its own line, as today, a campaign findings summary (nights observed, nights lost and why, the combined light curve, and what it does and does not show) and a campaign log. It extends the one-folder-per-mission package in 1.6.
- [ ] **Lose a night without failing.** A cloudy night is recorded as lost, with the reason, and the next window is planned. The campaign ends on its stop rule, not on the first miss.
- [ ] **A campaign page on the dashboard:** nights done, nights lost, the next planned visit and the combined light curve.

*Done when:* in simulation, a 15-night campaign on one star with several cloudy nights finishes; its folder holds one upload file with a line for every observed visit, a combined light curve and a findings summary that names each lost night; and a test shows a night observed with different settings is flagged and not merged. A campaign on the real telescope follows once Phases 2 and 3 are done.

#### Phase 5.4: Plan the night without a person

Today a person reads the Opportunities tab, picks a window, saves it as a plan and confirms it. The aim is that Astronomo proposes the plans and a person approves them. The planner here is rules in code, with no language model. It does not depend on the `generate_hypothesis` node: it works before a mission exists, and a mission started from a plan observes exactly that plan, so `generate_hypothesis` takes the plan's target as given, validates it and writes the rationale. The planner and a run started with no plan both draw on the plugin's `generate_targets`, so there is one candidate list. [Phase 7.1](#phase-7-autonomy) later lets a model rank the same candidates, with this planner as its fallback.

- [ ] **A planner.** On a schedule, or with a command such as `python main.py --plan-tonight`, it reads the same inputs as the Opportunities view (reachability, window, forecast, impact estimate, and the visits any campaign in 5.3 still needs) and writes proposed plans: ranked, with the reason for each. It only proposes. Nothing starts. The same inputs give the same proposals, and a test checks it.
- [ ] **A `PROPOSED` status for plans,** and a dashboard view of proposed plans to approve or reject. Approving uses the existing plan life cycle (`--confirm`). A rejected proposal is remembered, so it is not proposed again.
- [ ] **Limits in code.** A cap on plans per night and per week, no overlapping windows, the site interlocks and the weather forecast respected, and a rule that a proposal cannot lower any interlock.
- [ ] **Optional auto-approval, off by default.** A person can allow plans of one named kind (for example a campaign visit) to be approved without them, and only after the mount has been confirmed (`ASTRONOMO_UNATTENDED_CONFIRMED`, 5.1).

*Done when:* in simulation, with the model off, the planner turns a week of Opportunities and a forecast into proposed plans for a campaign and for ad hoc targets; a person approves one in the dashboard and it runs through `--from-plan`; nothing starts by itself unless auto-approval is on for that kind of plan and the mount is confirmed; and the same inputs give the same proposals.

#### Phase 5.5: Ready for another owner

- [ ] **Package the plate-solver indexes.** Fetch the Seestar index set (Tycho-2 4107 to 4112, about 345 MB) into a data folder and call the operating system's `solve-field`, so another owner does not need this Mac's install. The indexes stay out of the repository.
- [ ] **Contribution panel on the dashboard** (after [Phase 3](#phase-3-first-accepted-file)): timings and magnitudes prepared, reviewed, sent and accepted, from the manifest and a person's own record of acceptance.

*Done when:* a fresh machine solves a frame without this Mac's install, and the panel shows prepared, sent and accepted counts.

**Exit criteria (all required):**

- [ ] A multi-night mission starts, sleeps through bad weather, resumes and finishes with nobody touching it (5.1).
- [ ] The mid-sequence window case is handled (5.2).
- [ ] A campaign over many nights, with some lost to weather, finishes in simulation with one combined package, and a night observed with different settings is flagged and not merged (5.3).
- [ ] With the model off, the planner proposes tonight's plans and a person's approval starts one (5.4).
- [ ] The contribution panel shows prepared, sent and accepted counts, and a second owner can solve frames without this Mac's install (5.5).

### Phase 6: Transits

**Goal:** a known transit observed on the telescope with its mid-time inside its error (level A), a measured timing error, and transit timing as a regular product, kept only while its error bar holds.

**Why later:** a dip of 1 to 2 % is about the size of the noise on a 10-minute stack at V 11 to 12, a transit needs about four hours of tracking on an alt-az mount, and its timing error is not measured yet (the Opportunities page assumes 3 minutes). The variable-star visit is shorter and the signal is far larger, so it goes first. Nothing in Phases 1 to 5 needs a transit.

**Done (simulation):** the transit plugin with differential photometry, field-rotation correction, an AAVSO Exoplanet writer (`Rnflux`), the Opportunities transit lists with a first-light preset and a seven-day cloud forecast per window, and the staleness ranking from ExoClock, Exoplanet Watch and the NASA TTV flag (all fetched live; ExoClock 776 of 776 planets on 2026-11-14). Plugin page: [transit](docs/plugins/transit.md).

#### Phase 6.1: Observe a known transit (level A)

- [ ] Cover the mid-time and time on either side. Use a deep, well-known planet (the first-light preset). The run is only a verification if the answer is known beforehand. The checklist is [the first-transit runbook](docs/runbooks/first-transit.md).

*Done when:* the mid-time agrees with the published ephemeris within its combined error and within about 3 minutes.

#### Phase 6.2: Measure the timing error

- [ ] **Write the timing error down** (open hardware question 14) and replace `ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN` (default 3 minutes) in the impact estimate with it.
- [ ] **Error calibration:** the pull RMS over timings is about 1. Build a short O-C series on a handful of planets (for example 5 planets with at least 4 timings each over a season) rather than scattered points.

*Done when:* the Opportunities estimate uses the measured error, and the pull RMS is reported with the planets it came from.

#### Phase 6.3: An AAVSO Exoplanet report

- [ ] From the real frames, with the first frame attached and the target (T) and comparison stars (C) marked, through whatever check the AAVSO provides for exoplanet reports (confirm in 3.2 that one exists and that the report format is still current). The header has an empty `#DETREND_PARAMETERS=` that has not been checked.

*Done when:* the report passes the AAVSO's validator.

#### Phase 6.4: Field rotation over a long series

- [ ] Measure field rotation across the long series, re-set the rotation limits from it, and keep or refuse the light curve on the result.

*Done when:* the limits are set from a real series and the keep-or-refuse rule is tested.

#### Phase 6.5: Build a set of known answers

- [ ] **A known-answer set for transits:** a deep, clean real transit, a shallower real transit and a real night with no transit, so the tests cover both "does it miss a weak signal" and "does it invent one". Add a physical-model cross-check (limb-darkened, from published parameters). The pipeline's trapezoid duration reads short on a limb-darkened transit, so fit a physical shape or label the number as a trapezoid duration.

*Done when:* the tests cover a weak signal and an empty night, and the duration is either fitted or labelled.

#### Phase 6.6: Networks

- [ ] ExoClock asks for 5-inch-class telescopes and will not list a Seestar, so Astronomo applies its own brightness and depth limits. Find the route for Exoplanet Watch submissions.

*Done when:* the route for at least one network is written down.

**Exit criteria (all required):**

- [ ] A transit on a physical Seestar covers the mid-time, and the result agrees with the published ephemeris (level A) (6.1).
- [ ] The timing error is measured and the Opportunities estimate uses it (6.2).
- [ ] An AAVSO Exoplanet report from the real frames has passed the AAVSO's own validator (6.3).
- [ ] The decision rules hold: if the systematic timing error is above about 5 minutes and cannot be fixed, do not scale up transits and say so in the README. If the pull RMS is clearly above 1 and cannot be explained, fix the quoted errors or stop offering the file.

### Phase 7: Autonomy

**Goal:** give the language model a small amount of real authority inside the deterministic graph, safely; then test findings and loop; then measure which model to use. The graph stays the orchestrator and the safety spine ([why](docs/decisions.md#orchestration-deterministic-graph-not-an-llm-agent-current-state-2026-10)). Needs the claim levels from Phase 4 and the scheduler from Phase 5.

#### Phase 7.1: Bounded decisions

- [ ] Split the model call out of `generate_hypothesis` into its own node, so it is replaceable, skippable and testable alone. Add per-node model settings (`LLM_`* overrides).
- [ ] `propose_mission`: choose and order targets from the plugin's candidate list, as a model-assisted step in the planner ([5.4](#phase-54-plan-the-night-without-a-person)) and in a run started with no plan. A run started from a plan keeps its planned target. Schema-constrained output, validated in code, allowlist only, with the rule-based planner from [Phase 5.4](#phase-54-plan-the-night-without-a-person) as the fallback.
- [ ] Experiment design: extend it so the model can propose how to observe (exposure, frame count, cadence) within bounds each plugin declares. Code validates every value and falls back to the plugin's default plan.
- [ ] `evaluate_data`: after `reduce_data`, decide accept, re-observe once, or skip, from the digest only. The fallback is always accept.
- [ ] Show the model's rationale in the proposed-plan view that 5.4 builds (targets, order, rationale).

*Done when:* the split node, `propose_mission` and `evaluate_data` run in real and simulated missions, with invalid or missing output falling back to today's behaviour; the model never calls hardware, and a test enforces that; per-node model selection works; a plan outside a plugin's bounds is rejected and replaced.

#### Phase 7.2: Close the loop

- [ ] A post-analysis decision node with a fixed set of actions (`done`, `follow_up`, `redesign`, `drop`), schema-constrained, with `done` as the fallback.
- [ ] Hard limits: a maximum number of rounds per mission and per target, a wall-clock and token budget, and a rule that the loop cannot lower any safety interlock. The loop ends in code, never because the model says so.
- [ ] Confirmation observations: an independent re-observation is the test of a finding. Nothing is promoted past `unidentified candidate` without one.
- [ ] A rework record (why each round happened) shown on the mission page, and loop-aware state that keeps each round's observations separate.

*Done when:* in simulation, a mission follows up a marginal candidate, confirms or rejects it on the second round, and stops at the round limit if it does not converge; an invalid decision falls back to `done`; a test shows the loop cannot bypass any interlock; every mission log lists the rounds and the final claim level.

#### Phase 7.3: Measure the models

- [ ] Fixtures and scoring: expected-against-chosen target order, and accept or reject accuracy on labelled reductions.
- [ ] Same-task comparison (which model is best at a task) and cross-task comparison (one orchestrating model with others on subtasks).

*Done when:* a written result picks default models per node, with the fixtures committed so it can be repeated.

**Exit criteria (all required):**

- [ ] Phases 7.1, 7.2 and 7.3 are each done by their own "done when".
- [ ] A test shows that no model output can reach the hardware or lower a safety check.
- [ ] With the model switched off, every mission still behaves as it did before this phase.

### Phase 8: Reach

**Goal:** expand what the system can do. Each item is large or vague, or depends on trust earned in earlier phases. Define it properly before starting it. The exit criteria below are provisional, and are to be rewritten when Phase 7 is done.

**Done:** the frontend migration (2026-11): React over `/api/v1`, Streamlit removed.

#### Phase 8.1: Plugin directory UI

- [ ] List available plugins and what they do. Fold it into the Opportunities view rather than a separate page.

#### Phase 8.2: Sending, per network

The preparation is done. Depends on claim levels and confirmation observations.

- [ ] Check each format against the destination's own validator where one exists, and watch for format changes (AAVSO Exoplanet v0.62, Extended v1.2 and ADES 2017 were current when written, and the AAVSO is building new intake, see [3.2](#phase-32-find-out-what-the-aavso-wants-then-meet-it)).
- [ ] A human sign-off gate that records who approved which file, then optional sending (AAVSO WebObs, MPC).
- [ ] More destinations, each researched first: the Exoplanet Transit Database, the Transient Name Server, and any BAA list with a public endpoint.

#### Phase 8.3: More Pro-Am science cases

- [ ] Asteroid light curves, comets, and supernova or nova follow-up (level D, which reuses the variable-star photometry). The candidates and what each needs are in [science options](docs/notes/science-options.md#appendix-candidate-science-cases-for-a-50-mm-telescope). Rubin follow-up has not been run live, and most alerts are fainter than a 50 mm telescope reaches. The asteroid plugin proposes no anomalies, and the Rubin plugin screens only for outbursts.
- [ ] **A credible "unidentified" for asteroids** (moved here from the old Phase 4, because it needs the asteroid plugin on the real sky and that plugin is a draft): consult SkyBoT and the MPC, link visits across nights, and measure sensitivity on real frames. Without this, no discovery claim is allowed.

*Done when:* the case chosen has produced a file a network accepted, and any asteroid "unidentified" track in a real-sky run has been checked against SkyBoT and the MPC.

#### Phase 8.4: An MPC observatory code, only if the reach allows it

- **Decided 2026-11-20: Astronomo moves forward without an MPC code.** Nothing before this phase needs one, and the asteroid plugin keeps writing `DRAFT_` ADES files until one exists.

- [ ] The rule needs ten numbered near-Earth asteroids fainter than magnitude 16, each on two nights, which a 50 mm telescope may not reach. Measure the reach first. If it is realistic, build the qualifying ADES file: station `XXX`, the `Long./Lat./Alt.` comment, 3 to 5 positions each with magnitudes. Notes: [mpc-observatory-code.md](docs/notes/mpc-observatory-code.md).

#### Phase 8.5: Multi-telescope coordination

- [ ] Needs the scheduler and a decision about whether telescopes are separate missions or one plan.

**Exit criteria (provisional; all required):**

- [ ] A second science case (level D) has produced a file that a network accepted.
- [ ] A file has been sent through a network's real channel after a person's recorded sign-off (8.2).
- [ ] A plugin written by someone other than the author runs and is listed in the plugin directory (8.1).
- [ ] Each remaining item above is either built to its own "done when", or dropped here with a reason.

## Completed phases

Finished phases, kept short. The detail and the dates are in the [journal](docs/journal.md); the exit criteria stay here as the record that each phase closed.

### Phase 1: Public release

**Goal (met):** the repository can be made public without surprises. A stranger can install it, read an honest README, and know what is unproven.

**Closed 2026-11-21.** The one data set without a stated licence was removed. The README has a dashboard screenshot, a small architecture diagram and a status block audited against what has been seen on a real telescope. A NumPy 2.5 bug was fixed, CI covers Python 3.10 to 3.13 daily, and the README names the versions. The documentation has one home per topic, the plugins that are not ready are marked as drafts, and a mission writes its upload files, a findings summary and a mission log into one `results/` folder. The repository is public, private vulnerability reporting is on, and a ruleset protects `main` and requires `frontend` and `tests (Python 3.10)`. Every part, with its numbers: [journal](docs/journal.md#phase-1-public-release-as-completed-2026-11-21).

**Exit criteria (all required):**

- [x] Phases 1.1 to 1.6 are done, or waived by the owner in this file. Done 2026-11-21: every box in 1.1 to 1.6 is ticked and each *done when* is met.
- [x] A mission produces a findings package (upload files, findings summary, mission log) with no LaTeX needed (1.6). LaTeX is gone; the findings summary is open. Done 2026-11-21: `python main.py --simulate` into a scratch folder wrote the upload files, `findings_<night>.md` and `mission_log_<night>.md` into one `results/` folder, and the dashboard's API returned the findings with their table, caveats and checks.
- [x] The repository is public (1.7), and the main branch requires the tests to pass. Done 2026-11-21: the repository is public and the "Protect Main" ruleset requires `frontend` and `tests (Python 3.10)`.
- [x] A person who has only read the README can say what has been run on a real telescope and what has not. Done 2026-11-21: the README was read again after the 1.5 cut and the 1.6 change. Its status block says two short visits have finished and names what has not been seen (a clean 40-frame night, a whole mission over station mode, a live AAVSO file, a transit), and the plugin table marks the two drafts and `transit_finder` as simulation only.

## Backlog

Ideas worth keeping that block nothing. The first seven belong to the Opportunities page and the site profile, and each says what it would answer. The last three are loose ends from Phase 1.

- [ ] **Forecast overlay for the coming week.** Show the live forecast (cloud cover and wind, from the weather client a mission uses) next to each window in the next seven days, labelled with its age.
- [ ] **Climate odds per window.** From the site's history (Open-Meteo's historical cloud cover, cached per site and month), turn "a chance" into "a chance with odds" and show the expected number of usable windows. Both are estimates and are labelled so.
- [ ] **Sky darkness.** A Bortle class (typed in, cheapest) or a light-pollution atlas lookup (check the licence), to set how faint a target the site can reach, which today is one fixed V limit.
- [ ] **Where the network has gaps.** Ask whether this site's longitude covers transits few other observers can, if the networks publish their observer lists.
- [ ] **More than one site.** Several named profiles, one active, and each plan, mission and upload file remembering the site it was made for.
- [ ] **Ask the telescope.** Read the site from the telescope instead of typing it, and warn when it disagrees with the saved site. Needs the real hardware to check.
- [ ] **Measure the horizon instead of typing it.** Import a Stellarium landscape or a phone panorama, or take it from a night's plate-solved frames.
- [ ] **Mark the target on the findings page's finder image.** The finder image is the stretched first frame with no stars marked; the transit upload file already carries its own marked field image. A marked finder image needs the plate solution and the comparison stars the light curve used.
- [ ] **Revisit the two draft plugins.** `asteroid_finder` and `rubin_followup` are kept as drafts instead of deleted. Decide whether to delete them if keeping four plugins green slows Phases 2 to 5.
- [ ] **Look at the dashboard's findings view in a browser.** The Documents tab and the Summary link are covered by render tests and a scripted check of the API, and nobody has looked at them on a screen yet.

## Documentation map

Every document is listed here, with what it is for and where it connects to this roadmap. **Adding a document means adding a row.**


| Document                                                                               | What it is                                                                                                               | Roadmap link                                                                                                                                             |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [README.md](README.md)                                                                 | Front page: mission, status, quick start, plugins, phase list                                                            | [Mission](#mission), [Phases](#phases)                                                                                                                   |
| [CONTRIBUTING.md](CONTRIBUTING.md)                                                     | Ground rules, setup, how to help and report                                                                              | [Phase 1](#phase-1-public-release)                                                                                                                       |
| [SECURITY.md](SECURITY.md)                                                             | How to report a safety or security problem privately, and what is on purpose                                             | [Phase 1](#phase-1-public-release)                                                                                                                       |
| [docs/runbooks/aavso-observer-code.md](docs/runbooks/aavso-observer-code.md)           | AAVSO account, observer code, site and equipment                                                                         | [Phase 3](#phase-3-first-accepted-file): the AAVSO account, code, site and equipment                                                                     |
| [docs/notes/firmware-and-authentication.md](docs/notes/firmware-and-authentication.md) | Why the JSON-RPC handshake is not implemented, and which Alpaca server the driver uses                                   | [Phase 2](#phase-2-first-clean-night): the firmware decision                                                                                             |
| [docs/notes/mpc-observatory-code.md](docs/notes/mpc-observatory-code.md)               | MPC observatory-code rules and whether a 50 mm scope can meet them                                                       | [Phase 8](#phase-8-reach): the MPC code                                                                                                                  |
| [docs/hardware-questions.md](docs/hardware-questions.md)                               | Open hardware questions on the Alpaca path, including what the 2026-11-13 night answered                                 | [Phase 2](#phase-2-first-clean-night): the hardware rows                                                                                                 |
| [docs/runbooks/first-clean-night.md](docs/runbooks/first-clean-night.md)               | The runbook for the first clean night: a 40-frame variable-star visit on a real Seestar, and which results to write down | [Phase 2](#phase-2-first-clean-night): the clean night                                                                                                   |
| [docs/notes/science-options.md](docs/notes/science-options.md)                         | Every science option compared: success odds, value, and how much Astronomo already covers                                | [Phase 8](#phase-8-reach): which case to build next                                                                                                      |
| [docs/runbooks/first-transit.md](docs/runbooks/first-transit.md)                       | The first transit mission: what it needs, the checklist, and what has to be true before it is worth a night              | [Phase 6](#phase-6-transits)                                                                                                                             |
| [docs/connect-a-seestar.md](docs/connect-a-seestar.md)                                 | How a Seestar S50 Pro was connected: station mode, the router trap, the telescope's own Wi-Fi, the smoke test            | [Phase 2](#phase-2-first-clean-night): the network for the clean night                                                                                   |
| [docs/real-hardware.md](docs/real-hardware.md)                                         | What is untested, safety interlocks, smoke test, retries, setup checks                                                   | [Phase 2](#phase-2-first-clean-night)                                                                                                                    |
| [docs/real-data.md](docs/real-data.md)                                                 | Archive replays, mission clocks, live-client checks against real services                                                | [Phase 4](#phase-4-claims-you-can-defend), [Phase 5](#phase-5-unattended-and-automatic) and [Phase 6](#phase-6-transits): replays and live-client checks |
| [docs/configuration.md](docs/configuration.md)                                         | Every setting and where settings come from                                                                               | all phases                                                                                                                                               |
| [docs/architecture.md](docs/architecture.md)                                           | The full diagrams, how a mission runs, code layout, mission log writer, telemetry                                        | [Architecture evaluation](docs/decisions.md#architecture-evaluation-astronomo)                                                                           |
| [docs/api.md](docs/api.md)                                                             | Versioned HTTP API (`/api/v1`): the contract for any frontend                                                            | [Architecture evaluation](docs/decisions.md#architecture-evaluation-astronomo)                                                                           |
| [docs/status.md](docs/status.md)                                                       | What is real on a real telescope and what is not yet, and the eight-stage loop as it stands today                        | [Now](#now)                                                                                                                                              |
| [docs/journal.md](docs/journal.md)                                                     | Field journal: what happened on each real night, what was found, and the detailed notes behind the old task lists        | [Phases](#phases)                                                                                                                                        |
| [docs/mission.md](docs/mission.md)                                                     | What Astronomo is for, what it holds itself to, the two "how we will know" ladders, and the north-star goal              | [Mission](#mission)                                                                                                                                      |
| [docs/dashboard.md](docs/dashboard.md)                                                 | The dashboard's pages and design rules (colour, type, charts), and what is still open                                    | [Phase 1.5](docs/journal.md#phase-1-public-release-as-completed-2026-11-21): replaced the old design package                                                                                    |
| [docs/decisions.md](docs/decisions.md)                                                 | Why it is built this way (participation over discovery, a deterministic graph) and the architecture strengths and gaps   | [Phase 7](#phase-7-autonomy)                                                                                                                             |
| [docs/models.md](docs/models.md)                                                       | Choosing a language model, cost controls, the data firewall                                                              | [Phase 7](#phase-7-autonomy)                                                                                                                             |
| [docs/writing-a-plugin.md](docs/writing-a-plugin.md)                                   | The plugin contract and how to simulate your experiment                                                                  | [Phase 8](#phase-8-reach)                                                                                                                                |
| [docs/plugins/transit.md](docs/plugins/transit.md)                                     | The transit plugin: what its numbers mean                                                                                | [Phase 6](#phase-6-transits)                                                                                                                             |
| [docs/plugins/asteroid.md](docs/plugins/asteroid.md)                                   | The asteroid plugin                                                                                                      | [Phase 8](#phase-8-reach)                                                                                                                                |
| [docs/plugins/rubin.md](docs/plugins/rubin.md)                                         | The Rubin follow-up plugin                                                                                               | [Phase 8](#phase-8-reach)                                                                                                                                |
| [docs/plugins/variable_stars.md](docs/plugins/variable_stars.md)                       | The variable-star plugin: one magnitude per visit, and what a listed window is                                           | [Phase 2](#phase-2-first-clean-night) and [Phase 3](#phase-3-first-accepted-file)                                                                        |
| [docs/plugins/variable-star-feasibility.md](docs/plugins/variable-star-feasibility.md) | Why variable-star photometry is the stronger case than transits: evidence, the site census, and the risks still open     | [North star](docs/mission.md#north-star-take-part-in-collaborative-pro-am-science), [Phase 2](#phase-2-first-clean-night)                                |


