# 06. The first clean night

Written 2026-11-15, restructured 2026-11-20. This is the runbook for the first variable-star night that finishes with nothing assumed ([Phase 2](../../ROADMAP.md#phase-2-first-clean-night)). How to connect, and what can hurt the telescope, is in [Real hardware](../real-hardware.md) and [Connect a real Seestar](../connect-a-seestar.md). What the telescope says goes into the [open hardware questions](05-first-light-checklist.md). What happened on earlier nights, and why none of them counts as the clean night, is in the [field journal](../journal.md#the-nights). A transit is a later phase: [08-first-transit.md](08-first-transit.md).

## What counts as the clean night

A 40-frame variable-star visit (about 20 minutes: 40 frames of 10 s, with 20 s between them) that finishes on the telescope with nothing assumed:

- on the home network, with `ASTRONOMO_ASSUME_CLEAR` unset and a forecast the mission fetched itself;
- a star with a published magnitude, under about 70 degrees altitude, on a sky that is clear;
- the VizieR mirror reached during the run, so the AAVSO Extended draft is written on that run;
- the telescope parked at the end.

The two 12-frame Hat-P-23 visits of 2026-11-18 (plans #19 and #22) do not count. They were short, they ran under `ASTRONOMO_ASSUME_CLEAR`, and neither wrote a live file.

## Before the night

1. **The network.** The router must not be on `10.0.0.x`, the telescope must be in station mode with the Alpaca switch on, and the Seestar app must be closed. The steps, and the router trap, are in [Connect a real Seestar](../connect-a-seestar.md). Checked on 2026-11-20 at the bench: the telescope joined at `192.168.1.200`, Alpaca answered, and the computer reached Open-Meteo and the Harvard VizieR mirror at the same time. The address came from the router and was not reserved, so it may change. `seestar.local` resolved to it and `--hardware-check` passed with that name as the host, so `ASTRONOMO_SEESTAR_HOST=seestar.local` (the default) works. If the name fails, look the address up again. From the computer, before you go out:

   ```bash
   curl "http://seestar.local:32323/management/v1/configureddevices"
   curl -s -o /dev/null -w "%{http_code}\n" "https://api.open-meteo.com/v1/forecast?latitude=39.7&longitude=-105&hourly=cloud_cover"
   ```

   Both should answer (the second prints 200). If either fails here, fix it before the night.
2. **The plan.** On the Opportunities page, set **Kind of observation** to variable stars and save a plan for a star with a published magnitude. A window from that list is a usable night, described in [the variable-star plugin](../plugins/variable_stars.md). It is a night to measure one magnitude, because the list has a period and no time of minimum. That magnitude, within its quoted error, is the Visit row of [the concrete goal](../mission.md#the-concrete-goal-what-astronomo-can-meaningfully-contribute).
3. **A rehearsal.** `python main.py --from-plan X --simulate` leaves the plan as it is. The real command below is what changes it.

## The commands

Open the UI first, in its own terminal, and leave it open. Then check the telescope and start the plan:

```bash
python -m dashboard.api
ASTRONOMO_SEESTAR_TRANSPORT=alpaca python main.py --hardware-check
ASTRONOMO_SEESTAR_TRANSPORT=alpaca ASTRONOMO_MOCK_MODE=false python main.py --from-plan X
```

The UI is at the address `python -m dashboard.api` prints (`http://localhost:8765` unless that port is taken; set `ASTRONOMO_API_PORT` or pass `--port` to move it). The first frame is shown there beside the focus and pointing result, and the rest of the sequence waits until those checks finish. If the picture is a tree, a roof, dew, or cloud, stop with `python main.py --stop`. That parks the telescope and does not resume. Leave `ASTRONOMO_ASSUME_CLEAR` unset, and do not set `ASTRONOMO_VARSTAR_FRAMES` (that is the 12-frame short visit). Stay until the mission parks.

## How the first frame is checked

The first frame is plate-solved before the rest of the sequence. When `solve-field` is installed that solve is local and does not need Gaia. Otherwise the check matches the frame to Gaia, which needs a network path (station mode gives one; on the telescope's own Wi-Fi it needs ethernet or a phone tether). The target is accepted when it lands on the chip, and the solution is written into the frame so photometry can find a star that is not at the centre. A measured miss off the chip slews again, up to two corrections. A frame that will not solve stops the night instead of exposing the rest of the sequence. The reduction calls Gaia again after the telescope parks, and that step still needs the network. The scope's own plate solve will say it is not available: Alpaca has no solve call, and that line does not pass or fail the night.

This path ran on the sky on 2026-11-18. `solve-field` is on the path, and both Hat-P-23 frames solved locally. On the night, confirm each of these before treating it as a clean night:

- [x] The new frame's `PIXSCALE` is 2.39, not 1.195. Plans #19 and #22 both wrote 2.39. A solved scale near 1.2 means the old guess is back; the HD 189733 frames measured 2.24.
- [x] Frame 1 solves locally, with no Gaia call. Plan #19's kept frame was 15.2 arcmin from the command, target on the chip. Plan #22's kept frame had the target on the chip at pixel (986, 1817), after the correction below.
- [x] If the target is off the chip, the log contains `Pointing correction 1/` and the scope slews again, at most twice. Plan #19 did this once, from 161 arcmin off. Plan #22 did it once, from 56.6 arcmin off (target at pixel (−184, 2751)). Both kept frames have the target on the chip, with a WCS plus `TARGRA` and `TARGDEC`.
- [ ] If the target is already on the chip, the sequence continues and those same keywords are written, including when the star is far from the centre. Neither Hat-P-23 visit did this. Both first slews were off the chip.
- [ ] A frame that does not solve stops the mission before the rest of the exposures. Both visits solved, so this stop has not been seen.


## Fallback: the telescope's own Wi-Fi

With no router (a field site), join the telescope's own Wi-Fi instead. That network has no internet, so the computer loses every other network while it is joined. Do this first, while the computer still has internet, and fetch the forecast close to the time you join (it is kept for `ASTRONOMO_FORECAST_MAX_AGE_H`, default 12 h):

```bash
python main.py --fetch-forecast
```

Then join the telescope's Wi-Fi (address `10.0.0.1`) and run the same commands as above with `ASTRONOMO_SEESTAR_HOST=10.0.0.1`. The weather lookup will fail there, so the start command also needs `ASTRONOMO_ASSUME_CLEAR=true`, which marks the sky clear because you said so. A forecast that was actually read, including the saved one, is still enforced. The reduction calls Gaia again after the telescope parks, and that step needs the network, so a night run this way holds its AAVSO file until the computer has internet. A phone hotspot that both the telescope and the computer join is untried. So is a USB-tethered phone for internet beside the telescope's own Wi-Fi, which needs the tether above Wi-Fi in the macOS service order and breaks if the other network is also `10.0.0.x`.

## What the night records

Write the measurements in the Result column of the [open hardware questions](05-first-light-checklist.md#alpaca-path). The thresholds live there.

- [ ] `--hardware-check` passes, then the mission finishes and parks. Both visits reached `COMPLETE` and the plans are DONE. No hardware check was run before either visit (a bench check passed on 2026-11-20, but with `--no-park`), and the log does not record the park
- [ ] The weather gate reads a forecast the mission fetched, with `ASTRONOMO_ASSUME_CLEAR` unset. Both 2026-11-18 visits called Open-Meteo, the call failed on the telescope's own Wi-Fi, and `ASTRONOMO_ASSUME_CLEAR` marked the sky clear with no cloud, wind, or rain reading. Over station mode the call is reachable (bench check, 2026-11-20), and a mission has not used it
- [x] Focus runs on a clear frame (row 9): 2.2 px, 60 stars, after autofocus (2026-11-16, plan #17). Plans #19 and #22 passed at 4.9 px and 4.8 px, 60 stars
- [x] The first frame is plate-solved locally, and the offset, scale, rotation, and mirror are recorded (row 10). Gaia is the fallback when `solve-field` is missing. Both kept frames have `PIXSCALE` 2.39 and a solved WCS at 2.24 arcsec/pixel. The image is mirrored. Up is 144° west of north on plan #19 and 134° on plan #22, the same sense as the HD 189733 frames (136°). Offsets are the correction note above
- [ ] The dashboard shows that first frame beside the checks before the rest of the sequence. `first_frame.png` was written before the rest of the sequence on both visits. The log does not say the dashboard was open
- [x] Background, noise, gradient, and saturation are recorded (row 12). Plan #19: background 2528 ADU, noise 128 ADU, gradient 2%, 0.001% saturated. Plan #22: background 2496 ADU, noise 124 ADU, gradient 1%, 0.001% saturated
- [ ] The first frame is compared with the Seestar app's picture of the same field, including which way is up (row 18)
- [ ] The Bayer pattern, and whether the frame is a raw mosaic or already debayered, is noted from the file (row 19). Partly: on 2026-11-16 the header had no `BAYERPAT`, and the pattern is still unknown
- [ ] `lastexposurestarttime` is checked against a clock, and a frame's download time is noted (row 20). Partly: both nights wrote `DATE-OBS` with `TIMESRC` `host clock` (plan #19 frame 1 at 2026-11-19T01:11:46Z, frame 12 at 01:19:58Z). The scope's exposure-start time was not compared with a clock, and the download time was not measured
- [ ] The longitude sign is confirmed (row 21). Latitude and longitude already matched on 2026-11-13; the elevation write was refused
- [ ] Sun separation, Moon separation, altitude, and the mount's slew limit agree with a planetarium for this site and time (row 15)
- [ ] The free-space and dew checks run at the start and let a good night continue
- [x] A mission log is written from these frames. Both visits wrote `log/mission_log_2026-11-19.tex` (an earlier format; the log is now Markdown)

## The variable-star visit

- [x] The visit finishes on the telescope (plans #19 and #22, 2026-11-18, both 12 frames)
- [x] Its plan moves from PLANNED to STARTED to DONE
- [x] One magnitude is measured on a real frame. Plans #19 and #22 give 12.145 ± 0.057 and 12.139 ± 0.042. The header has no `BAYERPAT`, so this is the single-channel frame labelled `TG`, on a Gaia G zero point
- [x] The comparison stars come from a real Gaia match (Gaia DR3 through the Harvard VizieR mirror; the ESA archive timed out, and the telescope Wi-Fi cannot resolve the mirror)
- [ ] An AAVSO Extended file is written for that star. Both manifests are `held` and the file is absent. Plan #19's live finder reported no star within 45 px of the centre (the reanalysis later measured it). Plan #22 could not resolve `vizier.cfa.harvard.edu` on the telescope Wi-Fi
- [x] That magnitude is compared with the published value. Both nights sit within the quoted error of Gaia G 12.162. The catalogue value on the plan is Johnson V 12.35, and the plugin labels the result brighter than range
- [x] Field rotation on this short series is measured (row 11): 1.64 degrees on plan #19, 0.84 degrees on plan #22, and both were corrected in the reduction

## Still to decide, with this night's data

Two options are built and off by default, and one question is open. What was found on the earlier nights is in [the journal](../journal.md#making-the-data-usable-findings-from-the-v0381-and-and-hat-p-23-runs).

- [ ] **Focus slightly soft on purpose.** `ASTRONOMO_FOCUS_TARGET_FWHM_PX` (or `--focus-target`) settles where the fitted focus curve reaches that width, approached from below. Nobody has compared the scatter at the sharp and the soft focus on the same field. Done when that comparison is made. Try 3.5 on this visit, then again at 0.
- [ ] **Centre the target.** `ASTRONOMO_POINTING_RECENTRE_ARCMIN` (say 10) makes one correcting slew when the target is on the chip but further from the centre than that. It is off because the Seestar's goto error is random, so a correcting slew can land as far away as the first. Turn it on only if a night shows an off-centre start losing the target from the chip.
- [ ] **Does the slow trend recur?** The target gained about 0.12 to 0.15 mag on its comparison stars over a sequence on two nights. A flat built from the night's own frames did not touch it, and the attempt was removed. The plugin reports the trend in the findings. Done when a clean 40-frame night shows whether it recurs.

## Still open when the night has finished

The Seestar's own plate solve stays unanswered on Alpaca. The upload file has not been through the AAVSO's own page, and stays unsent until a person decides. Retries are shown only if the link drops and the mission continues. Leave `ASTRONOMO_UNATTENDED_CONFIRMED` unset. Row 13, the magnitude reached in one stack, is not measured by this software.

## After the night

Write what happened in three places, and nowhere else. First, a new entry at the top of [the nights in the field journal](../journal.md#the-nights). Second, the Result column of the [open hardware questions](05-first-light-checklist.md) for the rows the night answered. Third, the "Now" block and the tick boxes of [Phase 2 in the roadmap](../../ROADMAP.md#phase-2-first-clean-night). The README status block then gets one line pointing at the roadmap.
