# Roadmap

The plan: what each phase delivers, how it is built, and when it is done. **A phase is done when its exit criteria are met, not when every idea in it is explored.** Open questions are parked in the phase that needs the answer. Nothing in a later phase blocks an earlier one.

**Where things live.** This file is the plan, and the only place that says what is next and how each step is built. The mission and the goal: [mission](docs/mission.md). What is real and what is not: [status](docs/status.md). What happened on each night: [field journal](docs/journal.md). Why it is built this way: [decisions](docs/decisions.md). Every document in the repository is listed in the [documentation map](#documentation-map), and a test (`tests/test_docs_linked.py`) fails if one is missing or a link is broken.

## Now

*Last updated 2026-11-21. This block is the starting point when you come back. Update it first, and keep the detail in the phases below and in the journal.*

**Where we are.** Two short (12-frame) variable-star visits finished on a real Seestar S50 Pro on 2026-11-18 (Hat-P-23). The reanalysed magnitudes, 12.145 ± 0.057 and 12.139 ± 0.042, are inside the quoted error of Gaia G 12.162. Pointing correction, autofocus and a local plate solve have worked on the sky. On 2026-11-20 the telescope joined the home network in station mode, after the router was moved off `10.0.0.x`, and one computer reached the telescope, Open-Meteo and the Harvard VizieR mirror together. The hardware check passed. Not yet seen: a whole mission over station mode, a live AAVSO file, a transit that covers a mid-time.

**Next, in this order.**

1. [Phase 1, public release](#phase-1-public-release): the Python version matrix (1.3), the documentation consolidation (1.5), a findings package for each mission (1.6), and then going public (1.7). The README screenshot, the architecture diagram and the README audit are done (1.2). None of it needs the telescope, and it can run alongside Phase 2.
2. [Phase 2, first clean night](#phase-2-first-clean-night): keep the telescope awake while connected (2.1), build the recipe for repeating a night (2.2), then a 40-frame variable-star visit on the home network with nothing assumed (2.3).
3. [Phase 3, first accepted file](#phase-3-first-accepted-file): retry buttons (3.1), then a real file through the AAVSO's own checks (3.2).

Transits are [Phase 5](#phase-5-transits). Nothing before it needs one.

## Mission

**Help amateur astronomers contribute meaningfully to real science.** The principles, the two "how we will know" ladders (our own telescope and adoption) and the north-star goal are in [mission](docs/mission.md). The levels named below (Visit, A, B, C, D) are defined in [the concrete goal](docs/mission.md#the-concrete-goal-what-astronomo-can-meaningfully-contribute).

## Phases

Seven phases. The order is the path: **a public repository, then a clean night, then a file a network accepts, then unattended running, and only then transits.** Phase 1 does not need the telescope, so it runs alongside Phase 2. Phases 2 and 3 are in sequence. Phases 6 and 7 are later and not yet scheduled.

**How to read a phase.** A top-level phase (Phase 3) has a goal, what is already done, its sub-phases (Phase 3.1, 3.2 and so on) and **exit criteria**: the phase is finished when all of its exit criteria are true, and not before. A sub-phase is one piece of work: a checklist of tasks and a *done when*. Tick a task when it is done, and a phase when its exit criteria are. Where a task from the old phase lists went is in the [field journal](docs/journal.md#the-old-phase-lists-phases-1-to-6-as-of-2026-11-20).

### Phase 1: Public release

**Goal:** the repository can be made public without surprises. A stranger can install it, read an honest README, and know what is unproven. This phase does not wait for a clean night: the README says what has and has not been seen.

**Done:** Apache-2.0 `LICENSE` and `NOTICE`; the hardware use-at-your-own-risk disclaimer; the not-affiliated statement; `CONTRIBUTING.md`, `SECURITY.md` and the issue forms; the `.agents/` decision (ignored); this roadmap inside the repository; `pyproject.toml` with an `astronomo` command and `install.py`; settings validated at startup; CI (first green run 2026-11-14, in the repository this one was recreated from): pushes run the short suite, and the full suite runs when Python, the requirements, the test config or the workflow changes, and daily at 06:00 Mountain Standard Time.

#### Phase 1.1: Settle the third-party data question

- [x] **Remove the one data set without a stated licence.** The transit archive replay downloaded a third party's practice images. Its owner was asked and has not answered, so the adapter, its validation script, its tests and its documentation were removed (2026-11-20). Nothing in the repository downloads or credits that data. What remains uses public services under their own terms: ZTF through IRSA, the Fink broker, ExoClock, NASA Exoplanet Watch and the NASA Exoplanet Archive, the AAVSO and the MPC.

*Done when:* the owner has chosen, and the tree matches the choice. Done 2026-11-20.

#### Phase 1.2: Show what it does

- [x] **Architecture diagram:** the mission graph, the plugin boundary, the data firewall and the model's role, as two Mermaid diagrams in the README ([Architecture](README.md#architecture) and [How a mission runs](README.md#how-a-mission-runs)). Done 2026-11-20. GitHub draws them; the text carries the same facts for anywhere that does not.
- [x] **A README screenshot:** the dashboard's page for a finished simulated mission (result, what it means, the file to review, the light curve), at `docs/images/dashboard-mission.png`, captioned as practice data. Done 2026-11-21. It was taken from a mission run into a throwaway data folder, so it shows no real site or night.
- [x] **Audit the README against what has been seen.** Done 2026-11-21: the phase list matches Phases 1 and 4 here; the intro says the asteroid and Rubin plugins are drafts; `transit_finder` reads "ready (simulation only)"; "Not yet seen" includes a whole mission over station mode; station mode is called bench-tested only; stale test timings, a broken link and a stray markup glitch were fixed.

*Standing rule, not a task:* the README's status block and limitations say what has been seen on a real telescope and what has not. Keep its phase list in step with this file. It has no checkboxes or dates on purpose, so it should rarely change. Re-read it against this file whenever a phase closes.

*Done when:* the screenshot and the diagram are in the README and a reader of the README alone would not think more is proven than is. Done 2026-11-21.

#### Phase 1.3: Say which Python versions work

- [ ] Tested on 3.10 only. Test 3.11 and 3.12 in CI and state what is supported.

*Done when:* CI runs the matrix, and the README names the versions that pass.

#### Phase 1.4: Restructure the documentation

- [x] Slim this roadmap, the first-mission note and the README, and move the history out of them. Reviewed and committed (2026-11-20); `tests/test_docs_linked.py` passes.

*Done when:* the changes are reviewed and committed, and `tests/test_docs_linked.py` passes.

#### Phase 1.5: Consolidate the documentation

The repository has about 35 Markdown files. Phase 1.4 moved history out of the roadmap, the first-mission note and the README. This phase makes the whole tree easy to find your way around before strangers see it.

- [x] **Replace the dashboard design package** (seven files and five Streamlit screenshots, about 1,700 lines and 3.4 MB, all describing the Streamlit app that no longer exists) with the short [dashboard guide](docs/dashboard.md), checked against `frontend/src`. Done 2026-11-20. Two things it found that are still open: the IBM Plex fonts load from Google Fonts, so they fall back with no internet at the telescope, and `dashboard/vocab.py` still holds hex colours for mission states.
- [x] **Move the mission out of the roadmap** into [mission](docs/mission.md). Done 2026-11-20.
- [ ] **One home per topic.** `configuration.md`, `real-hardware.md`, `real-data.md` and `architecture.md` each explain setup and settings from a different angle. Decide where each setting and each setup step lives, and link to it from the others.
- [ ] **Sort `docs/first-light/` into runbooks and notes.** The numbered notes mix how-tos (06, 08), open questions (05) and one-off notes (01 to 04, 07). Name and group them by what they are, not by when they were written.
- [ ] **README to about 150 lines.** Move the long Known limitations section to [status](docs/status.md), and the connection steps to one page.
- [x] **Quick start uses a variable-star visit,** the case that works on this telescope, instead of Rubin and asteroid examples: the sample run is `--plugin variable_stars --simulate --fast`, the steps are numbered 1 to 6, and `variable_stars` leads the plugin table. Done 2026-11-20.
- [x] **Mark the plugins that are not ready as drafts** (`asteroid_finder`, `rubin_followup`) instead of deleting them: a `status = "draft"` attribute on the plugin class, a status column in the README table, a banner on each plugin page, `--list-plugins` and the mission header and log saying "DRAFT", and a refusal to command a real telescope unless `ASTRONOMO_ALLOW_DRAFT_PLUGINS=1`. The default plugin is now `variable_stars`. Done 2026-11-20. Revisit deleting them if keeping four plugins green slows Phases 1 to 4.
- [ ] **General README cleanup:** out-of-date numbers, repeated explanations, tables that run past the screen, the long Known limitations section, and anything that reads like a changelog. This is the same work as getting the README to about 150 lines.

*Done when:* every topic has one home, no two pages explain the same setting, the README is about 150 lines and its examples are variable-star visits, and the docs test passes.

#### Phase 1.6: Package the result

The goal is to package results for submission to the citizen-science networks. A mission writes upload files and a Markdown mission log; what is left is the findings package that sits beside them.

- [x] **The write-up step is gone (2026-11-20):** the `generate_paper` node, the paper policy, the `Paper` database model, the plugin `paper_context` hook, the LaTeX writer, the PDF output and the Jinja2 and TeX dependencies. Old databases keep an unused `papers` table. The `WRITING` status stays and means "writing the mission log".
- [x] **The mission log is a Markdown page** (`log/mission_log_<night>.md`, with a `figures/` folder) with the same content as before: targets and who chose them, what each observation found, files and what blocks them, the anomaly screen, errors, the observing log and a reproducibility table. `writer/markdown_writer.py` renders it.
- [x] **An anomaly is a flag in the mission log.** The anomaly screen keeps its threshold (default 5 sigma, enforced in core). When it fires, the log lists the evidence for a person to review; ones below the threshold are listed too. Nothing is sent. `tests/test_outputs_rubin.py` checks it.
- [x] **Update the docs** that described the old output: the README, the [mission](docs/mission.md) page, the architecture, configuration, models and plugin pages.
- [ ] **A findings summary per mission,** a plain Markdown page next to the mission log and shown in the dashboard: what was measured, its quoted error, how it compares with the published value where there is one, the finder image, and what a person must check before sending. The old write-up carried a few caveats of this kind for each plugin (which ephemeris was used, aperture dependence, "not a discovery"); they are not in the mission log yet and belong here.
- [ ] **One folder per mission that a person can open:** the upload files (with their `DRAFT_`, `SIMULATED_` or ready status and what blocks them), the findings summary and the mission log together. Today `submissions/` and `log/` are separate folders.

*Done when:* `python main.py --simulate` produces the upload files, the findings summary and the mission log in one folder, on a machine with no TeX engine; the dashboard shows the findings summary and the files; and a test checks that the summary carries each plugin's caveats.

#### Phase 1.7: Go public

- [ ] **Check the recreated repository first.** On 2026-11-21 the repository was recreated with one clean commit (the old one is kept privately). Turn private vulnerability reporting on again (it was on, 2026-11-14), confirm the `tests` workflow runs and passes on the new repository, and recreate any old issue that is still wanted (three were recreated; the rest stay in the old repository).
- [ ] Make the repository public.
- [ ] **Branch protection on `main`:** block force-push and deletion, and require the `tests` check. Tried 2026-11-14: GitHub returned 403, "Upgrade to GitHub Pro or make this repository public". It waits on the plan or on going public.

*Done when:* the repository is public and protection is set.

**Exit criteria (all required):**

- [ ] Phases 1.1 to 1.6 are done, or waived by the owner in this file.
- [ ] A mission produces a findings package (upload files, findings summary, mission log) with no LaTeX needed (1.6). LaTeX is gone; the findings summary is open.
- [ ] The repository is public (1.7), and the main branch requires the tests to pass.
- [ ] A person who has only read the README can say what has been run on a real telescope and what has not.

### Phase 2: First clean night

**Goal:** one supervised variable-star visit that finishes with nothing assumed, and leaves a recipe that someone else could repeat. The order and commands are in [the first-mission note](docs/first-light/06-first-mission.md).

**Done:** connect and hardware check (2026-11-13, again over station mode 2026-11-20); the pointing check, a local plate solve and a corrected slew (2026-11-18, twice); autofocus (2026-11-16); the first-frame view and per-frame watch; a colour term; the 70 degree altitude cap; two 12-frame visits that finished and matched Gaia G (2026-11-18). On 2026-11-20 the telescope was reached over the home network, with the internet reachable at the same time (bench only). Detail: [journal](docs/journal.md).

#### Phase 2.1: Keep the telescope awake while connected

**Why:** a clean night must not end because the scope went to sleep or dropped the connection while the computer was busy or waiting (a weather wait, a slow reduction call, a pause between steps). Today the controller sends a status ping every 8 seconds during long waits inside a mission (`HEARTBEAT_S` in `tools/telescope.py`, marked there as unconfirmed that it is needed). It is a fixed constant, not a setting, and nothing says what the S50 itself does when it is left idle (its own auto-off or sleep timer is not recorded in the [hardware questions](docs/first-light/05-first-light-checklist.md)).

- [ ] **Find out what the S50 does when idle,** over Alpaca and station mode: how long until it sleeps, powers down or drops the connection, with the heartbeat off and on. Write the answer into the hardware questions.
- [ ] **Keep it awake for as long as Astronomo is connected,** including waits that today have no ping (between plan start and slew, a weather wait while still connected, a slow outside call during reduction).
- [ ] **An option to turn it off** (a setting, on by default, for example `ASTRONOMO_SEESTAR_KEEP_AWAKE`), for an owner who wants the scope to sleep or who has the app's own timer set. It is listed in [the settings page](docs/configuration.md), and the mission log says which way it was set.
- [ ] **Decide what a long weather wait does.** Keep the scope connected and awake, or park and release it, and wake it later. Park-and-release is safer for the mount and the battery.

*Done when:* in simulation a test shows the ping runs during every kind of wait and stops when the setting is off. On the real scope, a 30-minute idle connection stays alive with the setting on, and what happens with it off is written down.

#### Phase 2.2: A recipe for repeating the observation

**Is this the same as the mission log?** No, though they share a source. The mission log is the account of one night after the fact: what the telescope did and what came of it. A recipe is the opposite direction: everything another person needs to make the same observation, and to know whether they got the same thing. Today the mission record holds much of this but nothing gathers it, and nothing can be turned back into a plan. The recipe is a section of the 3.2 package next to the log, and a file a program can read.

- [ ] **What the recipe holds:** the target (name, coordinates, which network list chose it and why), the site, the observing window and the conditions that were accepted, the equipment (telescope model, firmware version, camera settings), the exposure, gain, frame count and cadence, the focus and pointing steps taken and what they measured, the comparison stars, the reduction settings, and the Astronomo version, the settings that were in force (which `Settings.public_dict()` already gathers) and identifiers for the input data (catalogue, release and date).
- [ ] **A human-readable page and a machine-readable file** (`recipe.json`). The page also says which manual steps a person does on the telescope, linking [the runbook](docs/first-light/06-first-mission.md) rather than copying it.
- [ ] `**python main.py --plan-from-recipe FILE`** creates a plan with the same settings, so a second person (or the same one, next season) repeats the night.
- [ ] This also covers the reproducibility record that was in [Phase 4.3](#phase-4-steady-and-unattended): code version, config and input data identifiers are stored with each result.

*Done when:* in simulation, a mission's recipe loads into a plan whose settings match the original, a test covers it, and the real clean-night mission has a recipe that was read by someone who did not run it.

#### Phase 2.3: Run the clean visit

- [ ] On the home network, with `ASTRONOMO_ASSUME_CLEAR` unset: 40 frames, a target under about 70 degrees, a forecast the mission fetched itself, the VizieR mirror reached during the run, an AAVSO Extended draft written on that run, and the telescope parked. It is the first time a whole mission runs over station mode. Also unseen: whether the address and the Alpaca switch survive a power cycle. If `seestar.local` stops resolving, look the address up again.

*Done when:* the visit finished with nothing assumed, and the magnitude agrees with the published value within its quoted error, or the disagreement is explained.

#### Phase 2.4: Answer the open hardware questions

- [ ] **Fill the rows this night answers** ([open hardware questions](docs/first-light/05-first-light-checklist.md)): the Bayer pattern (row 19), the exposure-start time against a clock (row 20), the longitude sign (row 21), the picture compared with the Seestar app's (row 18), Sun, Moon, altitude and the mount's slew limit against a planetarium (row 15), the free-space and dew checks letting a good night continue, and the dashboard seen showing the first frame. Alpaca selects no filter and has no plate-solve call, so those rows stay open by design. Close the firmware decision box when the Alpaca rows are filled.
- [ ] **See the two cases not yet seen:** a first slew that is already on the chip, and an unsolved frame that stops the night.

*Done when:* every row this night can answer is answered, and both cases have been seen or are written down as not seen.

#### Phase 2.5: Settle the guessed settings

- [ ] **Decide the two options that are off by default,** with data from this night: a deliberately soft focus (`ASTRONOMO_FOCUS_TARGET_FWHM_PX`, try 3.5 then 0) and a recentring slew (`ASTRONOMO_POINTING_RECENTRE_ARCMIN`, only if an off-centre start loses the target from the chip).
- [ ] **Find out whether the slow trend recurs.** The target gained about 0.12 to 0.15 mag on its comparison stars across a sequence on two nights. Flat frames did not fix it, and the attempt was removed.
- [ ] **Re-set the guessed thresholds from real frames:** focus width, pointing, and the rotation limits (`warn_rotation_edge_px` 1.5, `max_rotation_edge_px` 4).

*Done when:* each option and threshold is either re-set from data or labelled as still a guess in the README.

**Exit criteria (all required):**

- [ ] A 40-frame variable-star visit has finished on a physical Seestar, supervised, with nothing assumed, on a star with a published magnitude, and the magnitude agrees within its quoted error or the disagreement is explained (2.3).
- [ ] The driver, frame retrieval and parking are confirmed against real firmware, and the README no longer says "untested" for them. Pointing and focus are verified on the run, and a failed check stops the mission with a clear message (2.3, 2.4).
- [ ] Weather, Sun, Moon, window and altitude checks have been seen working on the real mount, not only in simulation (2.4).
- [ ] A transient network or telescope error does not kill a mission on its first occurrence (built; unproven on a real fault).
- [ ] The telescope stays awake for the whole visit with keep-awake on, and it can be turned off (2.1).
- [ ] The clean night's mission has a recipe that loads into a plan with the same settings, and someone who did not run the night has read it (2.2).
- [ ] The README's limitations are updated with what the run showed (2.5).

### Phase 3: First accepted file

**Goal:** a real AAVSO Extended file from a real night is accepted by the AAVSO (level B), reviewed and sent by a person, and a failed outside call never leaves a night stranded. The findings package it is sent from is built in [Phase 1.6](#phase-1-public-release) and the recipe in [Phase 2.2](#phase-2-first-clean-night).

**Done:** the AAVSO observer code, site and Seestar equipment are in the account (2026-11-14). The writer emits AAVSO Extended (`TG` filter code) and a finder image with the target and comparison stars marked. A missing code keeps a file a `DRAFT_`, and simulated files stay `SIMULATED_`.

#### Phase 3.1: Retry buttons

A mission must not sit in limbo because an outside service failed. Today a call to a third party that fails mid-mission can leave a mission half-finished while the data to finish it is already on disk. On 2026-11-18 plan #22 ended that way: the 12 frames were saved, the live reduction could not resolve `vizier.cfa.harvard.edu`, the AAVSO file was `held`, and the magnitude appeared only after `python main.py --reanalyze` was run by hand on another network. The goal is that a failed outside call is a visible, recoverable state, and that a mission can be analysed whenever the data can eventually be fetched. Nothing here changes what is sent: a person still decides, and a retry never sends a held file.

- [ ] *Name the state.* Record on the mission which outside call failed (the service, what it was for, when, the error), and show it as its own state ("waiting on Gaia", "waiting on the forecast") instead of a generic hold. It is saved with the mission, so it survives a restart.
- [ ] *A Retry button on the mission page, and `python main.py --retry MISSION_ID` for a terminal.* It repeats only the failed step, from the saved frames and data. It never touches the telescope and never re-takes frames, and it is not offered for a step that needs the mount. It builds on `--reanalyze`, the node retries (`ASTRONOMO_NODE_RETRIES`) and the `held` manifest status.
- [ ] *A retry that works finishes the same mission.* Its results and files update in place, the file takes the status its normal rules give it (for example `held` to `draft`), and the log says it was a retry. A retry that fails again keeps the state and the new error. A person presses the button; nothing retries forever on its own.

*Done when:* in simulation, a mission whose Gaia, VizieR or Open-Meteo call fails ends in the named waiting state, the Retry button completes it once the service answers, and a test covers both. On a real night, switch the computer's network off during the reduction on a rehearsal, switch it back on, press Retry, and write down what happened.

#### Phase 3.2: Put a real file through the AAVSO

- [ ] **Run the file through the AAVSO's own check.** The writer is checked against the published format, not against the AAVSO's upload page. The finder image has not met a real Seestar frame.
- [ ] **A person reviews the package, uploads and records the acceptance.** Astronomo cannot know what a network accepted, and does not guess.

*Done when:* one real file is accepted, reviewed and sent by a person, and the AAVSO's response (accepted, rejected or warned, and why) is written in the journal.

#### Phase 3.3: Decide on the AAVSO's comparison-star service

- [ ] Comparison stars are a Gaia ensemble today, not a VSP chart, and a window from the Target Tool is a night, not an eclipse. Calling VSX (period, epoch) and VSP (comparison stars) would fix both. Decide from what the AAVSO asks for in 3.2.

*Done when:* a decision is written here, with the reason.

**Exit criteria (all required):**

- [ ] One real upload file is accepted by the AAVSO, reviewed and sent by a person (level B), and the acceptance is written in the journal (3.2).
- [ ] In testing, a mission with a failing outside call ends in a named waiting state and the Retry button finishes it (3.1).
- [ ] The file's own failure modes are known: what the AAVSO rejected or warned about, written down (3.2).

### Phase 4: Steady and unattended

**Goal:** visits run without someone watching, and their science claims survive scrutiny (level C). Still no language-model changes.

**Done (built and tested in simulation; not seen on the mount):** `python main.py --schedule` lists due plans and resumes a weather wait, and a plan starts only with `--confirm ID` (self-start is off and refused unless `ASTRONOMO_UNATTENDED_CONFIRMED` is set); the plan life cycle and the hand-off from a plan (`--from-plan ID`); rescheduling on weather; free-space, dew and battery checks; the site profile and interlocks; the React dashboard over FastAPI with the Opportunities view, which lists network targets with reachability, a window and a labelled impact estimate, and saves a window as a plan.

#### Phase 4.1: Self-start and multi-day missions

- [ ] **Self-start, only after the owner confirms the mount.** `ASTRONOMO_UNATTENDED_CONFIRMED` is the record that the interlocks and parking have been seen on this mount. Nothing in the repository sets it.
- [ ] **Multi-day missions.** Depends on the scheduler. Watch checkpoint growth (`observations` accumulates in state) and move bulky data out of state if it gets large. This is one mission that survives days. A goal pursued over many nights, with the nights combined, is a campaign ([Phase 4.7](#phase-47-campaigns-one-goal-over-many-nights)).

*Done when:* a multi-night mission starts, sleeps through bad weather, resumes and finishes with nobody touching it.

#### Phase 4.2: Honest photometry

- [ ] Report aperture dependence as a systematic error, use mid-exposure times, and calibrate the e⁻/ADU assumption for the Seestar.

*Done when:* results carry a quoted systematic error rather than a bare number.

#### Phase 4.3: Science-claim verification

A result may only be stated as strongly as the evidence allows, enforced in code, so neither a template nor the model can overstate it.

- [ ] *Claim levels* with explicit criteria (`detected`, `consistent with known object`, `unidentified candidate`, `independently confirmed`), shown in the mission log, upload-file notes, the findings summary and the dashboard. Nothing is promoted to `independently confirmed` automatically.
- [ ] *Known-answer regression tests* in CI: the ZTF archive replay and a variable-star known answer become committed tests with expected values and tolerances. Transit cases are in [Phase 5](#phase-5-transits).
- [ ] *Injection-recovery and null fields on real frames,* to measure sensitivity and the false-positive rate. Today this exists only on synthetic frames.
- [ ] *Human review gate:* nothing leaves the system without a person signing off on the claim level. The reproducibility record (code version, config, input data) is built in [Phase 2.2](#phase-2-first-clean-night).

*Done when:* every upload file and findings summary states a claim level computed in code from the evidence, known-answer regression tests run in CI, and a measured false-positive rate and sensitivity on real frames are quoted wherever a candidate is reported.

#### Phase 4.4: A credible "unidentified"

- [ ] Consult SkyBoT and MPC, link visits across nights, and measure sensitivity on real frames. Without this, no discovery claim is allowed.

*Done when:* an "unidentified" track in a real-sky run has been checked against SkyBoT and MPC.

#### Phase 4.5: Safety for unattended use

- [ ] A window that ends mid-sequence (the window is checked when a target starts), and an automatic obstruction mask (a person can draw a horizon by hand). Retries have only met simulated faults.

*Done when:* a run whose window closes mid-sequence stops cleanly and parks, seen on the mount or in a simulation that models it.

#### Phase 4.6: Ready for another owner

- [ ] **Package the plate-solver indexes.** Fetch the Seestar index set (Tycho-2 4107 to 4112, about 345 MB) into a data folder and call the operating system's `solve-field`, so another owner does not need this Mac's install. The indexes stay out of the repository.
- [ ] **Contribution panel on the dashboard** (after [Phase 3](#phase-3-first-accepted-file)): timings and magnitudes prepared, reviewed, sent and accepted, from the manifest and a person's own record of acceptance.

*Done when:* a fresh machine solves a frame without this Mac's install, and the panel shows prepared, sent and accepted counts.

#### Phase 4.7: Campaigns: one goal over many nights

Today a mission is one run: one night, one or a few targets. A **campaign** (a meta mission) is one goal pursued over many nights, for example 15 nights on one variable star, with the nights observed to the same recipe, combined, and packaged together. It builds on the scheduler (4.1), the recipe ([Phase 2.2](#phase-2-first-clean-night)), the findings package ([Phase 1.6](#phase-1-public-release)) and quoted systematic errors (4.2).

- [ ] **A campaign record.** The target or targets, the goal and its stop rule (a number of nights or visits, coverage of the star's cycle, or an end date), the cadence wanted (for example one visit a night) and the windows allowed. A campaign owns one ordinary mission per night. Each night's mission keeps its own full record and is never rewritten by the campaign.
- [ ] **Hold the setup fixed, and say when it is not.** The same channel, comparison stars, exposure and reduction settings on every night, taken from the recipe. A night observed with different settings is flagged and left out of the combination until a person says otherwise. It is never merged silently.
- [ ] **Combine the nights.** One light curve across nights with every visit kept, and a per-night check that the comparison stars agree from night to night. A night that disagrees is flagged. Anything derived from the combination (a period, a time of minimum) carries the systematic error from 4.2. Nothing is averaged away.
- [ ] **Package the campaign.** One folder: an upload file with every visit as its own line, as today, a campaign findings summary (nights observed, nights lost and why, the combined light curve, and what it does and does not show) and a campaign log. It extends the one-folder-per-mission package in 1.6.
- [ ] **Lose a night without failing.** A cloudy night is recorded as lost, with the reason, and the next window is planned. The campaign ends on its stop rule, not on the first miss.
- [ ] **A campaign page on the dashboard:** nights done, nights lost, the next planned visit and the combined light curve.

*Done when:* in simulation, a 15-night campaign on one star with several cloudy nights finishes; its folder holds one upload file with a line for every observed visit, a combined light curve and a findings summary that names each lost night; and a test shows a night observed with different settings is flagged and not merged. A campaign on the real telescope follows once Phases 2 and 3 are done.

#### Phase 4.8: Plan the night without a person

Today a person reads the Opportunities tab, picks a window, saves it as a plan and confirms it. The aim is that Astronomo proposes the plans and a person approves them. The planner here is rules in code, with no language model. [Phase 6.1](#phase-6-autonomy) later lets a model choose and order among the same candidates, with this planner as its fallback.

- [ ] **A planner.** On a schedule, or with a command such as `python main.py --plan-tonight`, it reads the same inputs as the Opportunities view (reachability, window, forecast, impact estimate, and the visits any campaign in 4.7 still needs) and writes proposed plans: ranked, with the reason for each. It only proposes. Nothing starts. The same inputs give the same proposals, and a test checks it.
- [ ] **A `PROPOSED` status for plans,** and a dashboard view of proposed plans to approve or reject. Approving uses the existing plan life cycle (`--confirm`). A rejected proposal is remembered, so it is not proposed again.
- [ ] **Limits in code.** A cap on plans per night and per week, no overlapping windows, the site interlocks and the weather forecast respected, and a rule that a proposal cannot lower any interlock.
- [ ] **Optional auto-approval, off by default.** A person can allow plans of one named kind (for example a campaign visit) to be approved without them, and only after the mount has been confirmed (`ASTRONOMO_UNATTENDED_CONFIRMED`, 4.1).

*Done when:* in simulation, with the model off, the planner turns a week of Opportunities and a forecast into proposed plans for a campaign and for ad hoc targets; a person approves one in the dashboard and it runs through `--from-plan`; nothing starts by itself unless auto-approval is on for that kind of plan and the mount is confirmed; and the same inputs give the same proposals.

**Exit criteria (all required):**

- [ ] A multi-night mission starts, sleeps through bad weather, resumes and finishes with nobody touching it (4.1).
- [ ] Results carry a quoted systematic error rather than a bare number (4.2).
- [ ] Every upload file and findings summary states a claim level, computed in code from the evidence, and known-answer tests run in CI (4.3).
- [ ] A measured false-positive rate and sensitivity on real frames are quoted wherever a candidate is reported (4.3).
- [ ] An "unidentified" track in a real-sky run has been checked against SkyBoT and MPC (4.4).
- [ ] The mid-sequence window case is handled (4.5).
- [ ] The contribution panel shows prepared, sent and accepted counts, and a second owner can solve frames without this Mac's install (4.6).
- [ ] A campaign over many nights, with some lost to weather, finishes in simulation with one combined package, and a night observed with different settings is flagged and not merged (4.7).
- [ ] With the model off, the planner proposes tonight's plans and a person's approval starts one (4.8).

### Phase 5: Transits

**Goal:** a known transit observed on the telescope with its mid-time inside its error (level A), a measured timing error, and transit timing as a regular product, kept only while its error bar holds.

**Why later:** a dip of 1 to 2 % is about the size of the noise on a 10-minute stack at V 11 to 12, a transit needs about four hours of tracking on an alt-az mount, and its timing error is not measured yet (the Opportunities page assumes 3 minutes). The variable-star visit is shorter and the signal is far larger, so it goes first. Nothing in Phases 1 to 4 needs a transit.

**Done (simulation):** the transit plugin with differential photometry, field-rotation correction, an AAVSO Exoplanet writer (`Rnflux`), the Opportunities transit lists with a first-light preset and a seven-day cloud forecast per window, and the staleness ranking from ExoClock, Exoplanet Watch and the NASA TTV flag (all fetched live; ExoClock 776 of 776 planets on 2026-11-14). Plugin page: [transit](docs/plugins/transit.md).

#### Phase 5.1: Observe a known transit (level A)

- [ ] Cover the mid-time and time on either side. Use a deep, well-known planet (the first-light preset). The run is only a verification if the answer is known beforehand. The checklist is [the first-transit note](docs/first-light/08-first-transit.md).

*Done when:* the mid-time agrees with the published ephemeris within its combined error and within about 3 minutes.

#### Phase 5.2: Measure the timing error

- [ ] **Write the timing error down** (open hardware question 14) and replace `ASTRONOMO_TRANSIT_ASSUMED_ERR_MIN` (default 3 minutes) in the impact estimate with it.
- [ ] **Error calibration:** the pull RMS over timings is about 1. Build a short O-C series on a handful of planets (for example 5 planets with at least 4 timings each over a season) rather than scattered points.

*Done when:* the Opportunities estimate uses the measured error, and the pull RMS is reported with the planets it came from.

#### Phase 5.3: An AAVSO Exoplanet report

- [ ] From the real frames, with the first frame attached and the target (T) and comparison stars (C) marked, through the AAVSO's own validator. The header has an empty `#DETREND_PARAMETERS=` that has not been checked.

*Done when:* the report passes the AAVSO's validator.

#### Phase 5.4: Field rotation over a long series

- [ ] Measure field rotation across the long series, re-set the rotation limits from it, and keep or refuse the light curve on the result.

*Done when:* the limits are set from a real series and the keep-or-refuse rule is tested.

#### Phase 5.5: Build a set of known answers

- [ ] **A known-answer set for transits:** a deep, clean real transit, a shallower real transit and a real night with no transit, so the tests cover both "does it miss a weak signal" and "does it invent one". Add a physical-model cross-check (limb-darkened, from published parameters). The pipeline's trapezoid duration reads short on a limb-darkened transit, so fit a physical shape or label the number as a trapezoid duration.

*Done when:* the tests cover a weak signal and an empty night, and the duration is either fitted or labelled.

#### Phase 5.6: Networks

- [ ] ExoClock asks for 5-inch-class telescopes and will not list a Seestar, so Astronomo applies its own brightness and depth limits. Find the route for Exoplanet Watch submissions.

*Done when:* the route for at least one network is written down.

**Exit criteria (all required):**

- [ ] A transit on a physical Seestar covers the mid-time, and the result agrees with the published ephemeris (level A) (5.1).
- [ ] The timing error is measured and the Opportunities estimate uses it (5.2).
- [ ] An AAVSO Exoplanet report from the real frames has passed the AAVSO's own validator (5.3).
- [ ] The decision rules hold: if the systematic timing error is above about 5 minutes and cannot be fixed, do not scale up transits and say so in the README. If the pull RMS is clearly above 1 and cannot be explained, fix the quoted errors or stop offering the file.

### Phase 6: Autonomy

**Goal:** give the language model a small amount of real authority inside the deterministic graph, safely; then test findings and loop; then measure which model to use. The graph stays the orchestrator and the safety spine ([why](docs/decisions.md#orchestration-deterministic-graph-not-an-llm-agent-current-state-2026-10)). Needs the scheduler and claim levels from Phase 4.

#### Phase 6.1: Bounded decisions

- [ ] Split the model call out of `generate_hypothesis` into its own node, so it is replaceable, skippable and testable alone. Add per-node model settings (`LLM_`* overrides).
- [ ] `propose_mission`: choose and order targets from the plugin's candidate list. Schema-constrained output, validated in code, allowlist only, with the rule-based planner from [Phase 4.8](#phase-48-plan-the-night-without-a-person) as the fallback.
- [ ] Experiment design: extend it so the model can propose how to observe (exposure, frame count, cadence) within bounds each plugin declares. Code validates every value and falls back to the plugin's default plan.
- [ ] `evaluate_data`: after `reduce_data`, decide accept, re-observe once, or skip, from the digest only. The fallback is always accept.
- [ ] Show the model's rationale in the proposed-plan view that 4.8 builds (targets, order, rationale).

*Done when:* both nodes run in real and simulated missions, with invalid or missing output falling back to today's behaviour; the model never calls hardware, and a test enforces that; per-node model selection works; a plan outside a plugin's bounds is rejected and replaced.

#### Phase 6.2: Close the loop

- [ ] A post-analysis decision node with a fixed set of actions (`done`, `follow_up`, `redesign`, `drop`), schema-constrained, with `done` as the fallback.
- [ ] Hard limits: a maximum number of rounds per mission and per target, a wall-clock and token budget, and a rule that the loop cannot lower any safety interlock. The loop ends in code, never because the model says so.
- [ ] Confirmation observations: an independent re-observation is the test of a finding. Nothing is promoted past `unidentified candidate` without one.
- [ ] A rework record (why each round happened) shown on the mission page, and loop-aware state that keeps each round's observations separate.

*Done when:* in simulation, a mission follows up a marginal candidate, confirms or rejects it on the second round, and stops at the round limit if it does not converge; an invalid decision falls back to `done`; a test shows the loop cannot bypass any interlock; every mission log lists the rounds and the final claim level.

#### Phase 6.3: Measure the models

- [ ] Fixtures and scoring: expected-against-chosen target order, and accept or reject accuracy on labelled reductions.
- [ ] Same-task comparison (which model is best at a task) and cross-task comparison (one orchestrating model with others on subtasks).

*Done when:* a written result picks default models per node, with the fixtures committed so it can be repeated.

**Exit criteria (all required):**

- [ ] Phases 6.1, 6.2 and 6.3 are each done by their own "done when".
- [ ] A test shows that no model output can reach the hardware or lower a safety check.
- [ ] With the model switched off, every mission still behaves as it did before this phase.

### Phase 7: Reach

**Goal:** expand what the system can do. Each item is large or vague, or depends on trust earned in earlier phases. Define it properly before starting it. The exit criteria below are provisional, and are to be rewritten when Phase 6 is done.

**Done:** the frontend migration (2026-11): React over `/api/v1`, Streamlit removed.

#### Phase 7.1: Plugin directory UI

- [ ] List available plugins and what they do. Fold it into the Opportunities view rather than a separate page.

#### Phase 7.2: Sending, per network

The preparation is done. Depends on claim levels and confirmation observations.

- [ ] Check each format against the destination's own validator, and watch for format changes (AAVSO Exoplanet v0.62, Extended v1.2 and ADES 2017 were current when written).
- [ ] A human sign-off gate that records who approved which file, then optional sending (AAVSO WebObs, MPC).
- [ ] More destinations, each researched first: the Exoplanet Transit Database, the Transient Name Server, and any BAA list with a public endpoint.

#### Phase 7.3: More Pro-Am science cases

- [ ] Asteroid light curves, comets, and supernova or nova follow-up (level D, which reuses the variable-star photometry). The candidates and what each needs are in [science options](docs/first-light/07-science-options.md#appendix-candidate-science-cases-for-a-50-mm-telescope). Rubin follow-up has not been run live, and most alerts are fainter than a 50 mm telescope reaches. The asteroid plugin proposes no anomalies, and the Rubin plugin screens only for outbursts.

#### Phase 7.4: An MPC observatory code, only if the reach allows it

- **Decided 2026-11-20: Astronomo moves forward without an MPC code.** Nothing before this phase needs one, and the asteroid plugin keeps writing `DRAFT_` ADES files until one exists.

- [ ] The rule needs ten numbered near-Earth asteroids fainter than magnitude 16, each on two nights, which a 50 mm telescope may not reach. Measure the reach first. If it is realistic, build the qualifying ADES file: station `XXX`, the `Long./Lat./Alt.` comment, 3 to 5 positions each with magnitudes. Notes: [04-mpc-observatory-code.md](docs/first-light/04-mpc-observatory-code.md).

#### Phase 7.5: Multi-telescope coordination

- [ ] Needs the scheduler and a decision about whether telescopes are separate missions or one plan.

**Exit criteria (provisional; all required):**

- [ ] A second science case (level D) has produced a file that a network accepted.
- [ ] A file has been sent through a network's real channel after a person's recorded sign-off (7.2).
- [ ] A plugin written by someone other than the author runs and is listed in the plugin directory (7.1).
- [ ] Each remaining item above is either built to its own "done when", or dropped here with a reason.

## Backlog

Ideas worth keeping that block nothing. They belong to the Opportunities page and the site profile. Each says what it would answer.

- [ ] **Forecast overlay for the coming week.** Show the live forecast (cloud cover and wind, from the weather client a mission uses) next to each window in the next seven days, labelled with its age.
- [ ] **Climate odds per window.** From the site's history (Open-Meteo's historical cloud cover, cached per site and month), turn "a chance" into "a chance with odds" and show the expected number of usable windows. Both are estimates and are labelled so.
- [ ] **Sky darkness.** A Bortle class (typed in, cheapest) or a light-pollution atlas lookup (check the licence), to set how faint a target the site can reach, which today is one fixed V limit.
- [ ] **Where the network has gaps.** Ask whether this site's longitude covers transits few other observers can, if the networks publish their observer lists.
- [ ] **More than one site.** Several named profiles, one active, and each plan, mission and upload file remembering the site it was made for.
- [ ] **Ask the telescope.** Read the site from the telescope instead of typing it, and warn when it disagrees with the saved site. Needs the real hardware to check.
- [ ] **Measure the horizon instead of typing it.** Import a Stellarium landscape or a phone panorama, or take it from a night's plate-solved frames.

## Documentation map

Every document is listed here, with what it is for and where it connects to this roadmap. **Adding a document means adding a row.**


| Document                                                                                                 | What it is                                                                                                               | Roadmap link                                                                                                              |
| -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| [README.md](README.md)                                                                                   | Front page: mission, status, quick start, limitations                                                                    | [Mission](#mission), [Phases](#phases)                                                                                    |
| [CONTRIBUTING.md](CONTRIBUTING.md)                                                                       | Ground rules, setup, how to help and report                                                                              | [Phase 1](#phase-1-public-release)                                                                                        |
| [SECURITY.md](SECURITY.md)                                                                               | How to report a safety or security problem privately, and what is on purpose                                             | [Phase 1](#phase-1-public-release)                                                                                        |
| [docs/first-light/01-aavso-observer-code.md](docs/first-light/01-aavso-observer-code.md)                 | AAVSO account, observer code, site and equipment                                                                         | [Phase 3](#phase-3-first-accepted-file): the AAVSO account, code, site and equipment                                      |
| [docs/first-light/03-firmware-and-authentication.md](docs/first-light/03-firmware-and-authentication.md) | Why the JSON-RPC handshake is not implemented, and which Alpaca server the driver uses                                   | [Phase 2](#phase-2-first-clean-night): the firmware decision                                                              |
| [docs/first-light/04-mpc-observatory-code.md](docs/first-light/04-mpc-observatory-code.md)               | MPC observatory-code rules and whether a 50 mm scope can meet them                                                       | [Phase 7](#phase-7-reach): the MPC code                                                                                   |
| [docs/first-light/05-first-light-checklist.md](docs/first-light/05-first-light-checklist.md)             | Open hardware questions on the Alpaca path, including what the 2026-11-13 night answered                                 | [Phase 2](#phase-2-first-clean-night): the hardware rows                                                                  |
| [docs/first-light/06-first-mission.md](docs/first-light/06-first-mission.md)                             | The runbook for the first clean night: a 40-frame variable-star visit on a real Seestar, and which results to write down | [Phase 2](#phase-2-first-clean-night): the clean night                                                                    |
| [docs/first-light/07-science-options.md](docs/first-light/07-science-options.md)                         | Every science option compared: success odds, value, and how much Astronomo already covers                                | [Phase 7](#phase-7-reach): which case to build next                                                                       |
| [docs/first-light/08-first-transit.md](docs/first-light/08-first-transit.md)                             | The first transit mission: what it needs, the checklist, and what has to be true before it is worth a night              | [Phase 5](#phase-5-transits)                                                                                              |
| [docs/real-hardware.md](docs/real-hardware.md)                                                           | What is untested, safety interlocks, smoke test, retries, setup checks                                                   | [Phase 2](#phase-2-first-clean-night)                                                                                     |
| [docs/real-data.md](docs/real-data.md)                                                                   | Archive replays, mission clocks, live-client checks against real services                                                | [Phase 4](#phase-4-steady-and-unattended) and [Phase 5](#phase-5-transits): replays and live-client checks                |
| [docs/configuration.md](docs/configuration.md)                                                           | Every setting and where settings come from                                                                               | all phases                                                                                                                |
| [docs/architecture.md](docs/architecture.md)                                                             | Code layout, the mission log writer, telemetry, headless pipeline and bundled UI                                         | [Architecture evaluation](docs/decisions.md#architecture-evaluation-astronomo)                                            |
| [docs/api.md](docs/api.md)                                                                               | Versioned HTTP API (`/api/v1`): the contract for any frontend                                                            | [Architecture evaluation](docs/decisions.md#architecture-evaluation-astronomo)                                            |
| [docs/status.md](docs/status.md)                                                                         | What is real on a real telescope and what is not yet, and the eight-stage loop as it stands today                        | [Now](#now)                                                                                                               |
| [docs/journal.md](docs/journal.md)                                                                       | Field journal: what happened on each real night, what was found, and the detailed notes behind the old task lists        | [Phases](#phases)                                                                                                         |
| [docs/mission.md](docs/mission.md)                                                                       | What Astronomo is for, what it holds itself to, the two "how we will know" ladders, and the north-star goal              | [Mission](#mission)                                                                                                       |
| [docs/dashboard.md](docs/dashboard.md)                                                                   | The dashboard's pages and design rules (colour, type, charts), and what is still open                                    | [Phase 1.5](#phase-1-public-release): replaced the old design package                                                     |
| [docs/decisions.md](docs/decisions.md)                                                                   | Why it is built this way (participation over discovery, a deterministic graph) and the architecture strengths and gaps   | [Phase 6](#phase-6-autonomy)                                                                                              |
| [docs/models.md](docs/models.md)                                                                         | Choosing a language model, cost controls, the data firewall                                                              | [Phase 6](#phase-6-autonomy)                                                                                              |
| [docs/writing-a-plugin.md](docs/writing-a-plugin.md)                                                     | The plugin contract and how to simulate your experiment                                                                  | [Phase 7](#phase-7-reach)                                                                                                 |
| [docs/plugins/transit.md](docs/plugins/transit.md)                                                       | The transit plugin: what its numbers mean                                                                                | [Phase 5](#phase-5-transits)                                                                                              |
| [docs/plugins/asteroid.md](docs/plugins/asteroid.md)                                                     | The asteroid plugin                                                                                                      | [Phase 7](#phase-7-reach)                                                                                                 |
| [docs/plugins/rubin.md](docs/plugins/rubin.md)                                                           | The Rubin follow-up plugin                                                                                               | [Phase 7](#phase-7-reach)                                                                                                 |
| [docs/plugins/variable_stars.md](docs/plugins/variable_stars.md)                                         | The variable-star plugin: one magnitude per visit, and what a listed window is                                           | [Phase 2](#phase-2-first-clean-night) and [Phase 3](#phase-3-first-accepted-file)                                         |
| [docs/plugins/variable-star-feasibility.md](docs/plugins/variable-star-feasibility.md)                   | Why variable-star photometry is the stronger case than transits: evidence, the site census, and the risks still open     | [North star](docs/mission.md#north-star-take-part-in-collaborative-pro-am-science), [Phase 2](#phase-2-first-clean-night) |


