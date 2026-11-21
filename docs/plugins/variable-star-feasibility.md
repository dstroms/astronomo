# Variable-star feasibility

Written 2026-10-29. This is the case for variable-star photometry against exoplanet transits. Transits were what the repository was built around when this note was written. The mission now takes targets from the networks that want amateur data, and on this telescope variable-star photometry is the stronger case. How a visit runs now is the [variable-star plugin](variable_stars.md). Other science options are compared in [the science-options note](../first-light/07-science-options.md).

The photometry numbers below were not measured on this S50 Pro, except where a sentence names the 2026-11-18 night. Where a number comes from a different telescope, or from a model, it says so. On 2026-11-18 two Hat-P-23 visits (12 frames each) finished on this telescope. Reanalysis measured 12.145 ± 0.057 and 12.139 ± 0.042, inside the quoted error of Gaia G 12.162. The live AAVSO file was held: the telescope Wi-Fi has no internet. Those visits are not the clean 40-frame night. A simulated visit measures one differential magnitude per night on a Gaia G zero point, writes an AAVSO Extended file a person reviews, and times a minimum when the series contains one. A window from the Target Tool is still a usable night, not an eclipse: the list has a period and no time of minimum. Comparison stars are a Gaia ensemble in the frame, not a VSP chart.

## Compared with transits

Variable-star photometry has a much larger margin between the signal and the telescope's noise than transits do, a published route for exactly this telescope into a real network (the AAVSO), and it does not need the telescope to be driven by software. What remains is mostly whether this particular unit can take clean, unsaturated, unprocessed frames.

| Question | Transits | Variable stars |
|---|---|---|
| Size of the signal against one 10-minute stack | about 1 to 2 times the noise per stack (a 1 to 1.5 % dip at V 11 to 12) | About 25 times (the shallowest class, at its faintest) to several hundred times (table below) |
| Does a few-hundredths-of-a-magnitude systematic ruin it? | Yes, it can erase the event | No, for most of the list. It matters only for eclipse timing |
| Hours of unbroken tracking needed | Yes, every time | Only for eclipsing binaries and pulsators. Not for Miras, semi-regulars, young stars or outbursts |
| Needs software control of the telescope | Yes, to be autonomous | No. The app plus saved sub-frames plus offline reduction is enough for the science |
| A network that has told people to use this telescope | Partly (Exoplanet Watch) | Yes (AAVSO pages for the S50, `TG` filter code, VPhot) |

## What the Opportunities page showed

Run 2026-10-29 as `plugins/variable_stars/`. It reads the AAVSO Target Tool's public list, keeps the stars a colour camera can serve, and finds, for each, the nights in the next 30 when it is dark, high and clear of the Moon, using the same checks a mission uses. The counts are for one site, Lafayette, CO (39.99 N, 105.09 W, 1588 m), and a 30-day lookahead. Another site produces another list.

| | Count |
|---|---|
| Stars on the AAVSO list (one row per star, filters merged) | 1616 |
| Reachable from this site with a 50 mm telescope | **628** |
| Not reachable: never climbs above 30 degrees here | 480 |
| Not reachable: never brighter than V 13 | 468 |
| Not reachable: too bright, a 10 s frame saturates even at its faintest | 110 |
| Not reachable: no usable stretch of any night in 30 days | 69 |
| Not reachable: spectroscopy-only or R-only requests, and no listed brightness | 19 |
| Reachable and flagged **high priority** by the AAVSO | 63 |
| Reachable and **never observed** against the request on file | 28 |
| Reachable, but only while the star is near its maximum | 276 |
| Reachable, but saturates when it is at its brightest | 81 |

(1616 minus 628 reachable leaves 988 not reachable. The reasons above add to more than that, because a star can fail for several.)

By class, among the 628 reachable. Amplitudes are the catalogue's maximum and minimum from the Target Tool. The noise column is the model below at V 12, a typical brightness for this list.

| Class | Stars | Median amplitude (mag) | Hours of tracking the science needs |
|---|---|---|---|
| Semi-regular and irregular (SR, L) | 124 | 1.2 | minutes per night |
| Cataclysmic variables and novae (dwarf novae, nova-likes) | 122 | 6.5 | minutes per night |
| Eclipsing binaries | 100 | 0.47 | 3 to 6 h per eclipse |
| Mira | 91 | 6.6 | minutes per night |
| Young stars (T Tau, UXOR) | 75 | 2.4 | minutes per night |
| Pulsators (RR Lyrae, delta Scuti, Cepheid) | 37 | 1.0 | hours, for the shortest periods |
| Blazars | 14 | 4.4 | minutes per night |
| Other | 68 | 1.6 | varies |

The model's noise for a **10-minute stack** (about 46 frames) is 0.0047 mag at V 10, 0.0075 at V 11, 0.012 at V 12 and 0.019 at V 13. Against amplitudes of 0.5 to 6 mag, the ratio is 25 or more even for the shallowest class. These classes do not live or die on the noise.

### What the page does not tell you

* **It does not say what a star is doing tonight.** The Target Tool gives a catalogue range, not a current magnitude. "Only near maximum" (276 stars) means you may find it too faint.
* **For an eclipsing binary, a window is a night, not an eclipse.** The Target Tool has a period and no time of minimum. The page says so on each such card. The gap can be closed: `vsx.aavso.org` returned epoch, period and eclipse duration for Y Cyg when tested (period 2.99641 d, epoch 2457214.818, duration 12; the unit of the last needs confirming). Not built.
* **The ranking barely discriminates.** The AAVSO's "overdue" flag is almost always set (many stars show "last observed 13 months ago"), so most of the top of the list ties at the maximum score. Treat the order as a tidy-up, not advice.
* **Four exoplanet hosts are in the list** (for example Hat-P-23 and HD 80606) because the AAVSO runs campaigns on them. They are transit science in a variable-star list. At V 12.35, Hat-P-23's 1 % dip is about as large as the noise on a 10-minute stack, so they are not the stars to judge this by.
* **The noise model is a placeholder.** It is anchored to an AAVSO report for an S50 (below) and has never been checked against this S50 Pro.
* **Not checked:** weather, crowding, whether a comparison star exists, saturation of the comparison stars.

## What is known, and from where

| Claim | Evidence | Strength |
|---|---|---|
| The S50's green channel gives about 0.01 mag scatter on T CrB (V about 10) after averaging 10 frames of 10 s | An AAVSO example page by an observer (code EHEA), 2024-04-10; the data are in the AAVSO light-curve generator | One observer, an S50 (not a Pro), good evidence it works |
| The same implies about 0.032 mag per single 10 s frame at V 10 | Arithmetic on the line above (0.01 times the square root of 10) | Consistent with the next row |
| A 1024-frame S50 night on CY Aqr (V about 10.7) gave about 0.05 mag per frame; the model gives 0.044 | A single blog post dated 2026-10-14, using the S50's new on-device photometry mode | Weak: one blog, and it could not be checked against vendor documentation |
| Treating the green channel as V is good to about 0.02 mag per unit of colour difference between target and comparison | An AAVSO post of S50 transformation coefficients (Tv_bv = 0.020), one observer | Weak to moderate. The fit of that particular coefficient is poor (R squared 0.08), which is what a near-zero slope looks like, but it is still one person's data |
| The AAVSO accepts the Seestar's green channel under the code `TG`, and VPhot demosaics Seestar colour frames into TB, TG and TR | The AAVSO's own pages, quoted on the same example page by AAVSO staff | Good |
| The Target Tool list is public and machine readable | Fetched and parsed on 2026-10-29 (about 4 MB, 1616 stars) | Verified here |
| VSX (period, epoch) and VSP (comparison stars with V magnitudes) answer a script | `vsx.aavso.org` and `apps.aavso.org/vsp` returned data for Y Cyg, five comparison stars with V magnitudes and errors | Verified here, once. The `www.aavso.org` host returns a Cloudflare challenge to scripts, so do not rely on it |
| Times of minimum are collected by EccentriCAT-LITE, the B.R.N.O. O-C Gateway and the BAV | Found in this project's research. **Not re-checked for this note** | Moderate. Check how to submit before relying on it |
| An S50 Pro gave usable unfiltered photometry of a supernova (SN 2026aaiv, 2026-09-27/28: V about 12.93 against 13.00 from an S50 on the same night, 0.15 mag scatter against about 440 APASS stars, about 570 frames of 10 s) | One blog post, `chris.tsehome.com/sn-2026aaiv-measured-with-two-seestars/`. Pseudo-V from unfiltered frames, good to 0.1 to 0.2 mag by the author's own account | Weak, but the only S50 Pro photometry found. It suggests the Pro's saved frames are usable (risk 1), not that they are linear |
| **Anything else about the S50 Pro** | Nothing found: no green-channel `TG` work, no transformation coefficients, no timing test | **None.** Different sensor (8.3 MP against the S50's 2 MP), a different readout and, possibly, different on-board processing |

## What the risk looks like

Split by what software can repair.

### Risks that software cannot repair

These are about this unit.

1. **The Pro's raw sub-frames may not be clean enough.** Photometry needs unprocessed (or at least linear) data. If the firmware applies noise reduction, banding correction or non-linear stretching before it saves the sub-frames, the photometry could be systematically wrong, and nothing in the repository can fix that. The AAVSO example's author says the S50 "is producing calibrated files" and used them as they came, which is encouraging for the S50. Whether the Pro's saved frames are linear is **unknown**.
   * *Probability this ruins it: low to moderate (about 10 to 15 %). Cheap to find out.*
2. **Saturation and dynamic range.** At a fixed 10 s exposure, anything brighter than about V 6 to 7 saturates, so the 110 too-bright stars and the 81 reachable stars that saturate at maximum are lost or need defocus. The same applies to *comparison stars*. For Y Cyg (V 7.3 to 7.9), the VSP chart's brightest comparison star is V 6.6, which would be saturated by a Seestar-class setup. This shrinks the usable list, not the whole idea.
   * *Probability this ruins it: very low. It prunes the list.*
3. **A systematic floor above the white-noise one.** Real photometry has red noise (slowly varying errors from flat-fielding, vignetting, colour terms and sky). There are no flat frames. The model gives 0.005 to 0.02 mag per 10 minutes; AAVSO observers with this telescope report about 0.01 at V 10, which agrees, but a floor of 0.02 to 0.03 mag is plausible. For Miras, semi-regulars, young stars, outbursts and blazars that does not matter. For an eclipsing binary with a 0.15 mag eclipse it would.
   * *Probability this ruins the slow classes: very low. The fast classes: moderate.*

### Risks that are real but repairable

4. **A clean unattended night is still unproven.** On 2026-11-18 two supervised Hat-P-23 visits finished on an S50 Pro (firmware 9.75, Alpaca). They were 12 frames, the sky was an operator assertion, and the live file was held because that Wi-Fi has no internet. The 2026-11-13 capture saved 32 frames and then the link dropped. **For variable stars a dropped link does not defeat the science:** you can run the app, keep "save each frame" on, copy the sub-frames off and reduce them offline. The frames can still be taken in the app.
5. **Field rotation (alt-az mount).** For a Mira or an outburst, 10 to 30 minutes of frames per night is enough, and rotation over that time is small. For a multi-hour eclipse, the field turns and stars drift toward the corners where vignetting changes. A pipeline that measures every frame at its own plate-solved positions, rather than aligning and stacking, avoids most of that. The transit photometry this plugin reuses corrects a rotation about the frame centre. On the Hat-P-23 visits that was 1.64° and 0.84°. The limits are still guesses. An equatorial head would remove the rotation, at a cost and with its own, also unproven, control path.
6. **Comparison stars.** Needed for every measurement. The AAVSO's VSP service provides them, with V magnitudes and errors of a few hundredths (for Y Cyg, 0.03 to 0.1). Their own error can exceed the Seestar's noise, so the *calibrated* scatter may be dominated by the comparison stars, not the camera. The plugin uses a Gaia ensemble in the frame, not a VSP chart. If VSP is blocked later, a downloaded chart is the fallback, and it is manual.
7. **Pixel scale and crowding.** At 2.3 arcseconds per pixel, a star in a crowded field (a young star in a cluster, a Galactic-plane field) is blended. This removes some of the 628, it does not break the programme.
8. **Colour.** The green channel differs from Johnson V by an amount depending on star colour. Miras are very red. Their amplitudes are so large (median 6.6 mag) that an error of a few hundredths does not matter, but it would show in a careful light curve and the AAVSO may ask for a transformation. The published coefficients are for the S50, not the Pro.
9. **Timing for eclipsing binaries.** Frame timestamps come from the telescope's own clock, set from the phone, and the S50's behaviour is unconfirmed. A 30 s offset would not matter for a Mira and would ruin a time of minimum. The model's statistical floor on a time of minimum is 0.3 to 2 minutes (V 9 to 12, a 0.15 to 0.5 mag eclipse over 2.5 to 3 h of 13 s frames; Carter-style white-noise formula). A useful result needs a clock accurate to well under that, so treat eclipse timing as a stretch goal.

### Risks that are about this repository, not the telescope

10. **The simulation path exists; the real frames do not.** As of 2026-11-14 a visit measures one magnitude, times a minimum when the series contains one, and writes an AAVSO Extended file (`TG`). Comparison stars are a Gaia ensemble, not a VSP chart, and the zero point is Gaia G rather than Johnson V. Green-channel extraction is built. What a real Seestar frame looks like (Bayer order, linearity, whether the sub-frames are raw) is still unknown. The running description is the [plugin page](variable_stars.md).
11. **Network policy.** The AAVSO has been accepting these data since at least 2024, but it can change the rules for smart-telescope data and the observer has to enter the equipment correctly. Low risk, but not zero.
12. **There is a vendor feature in the way (or on your side).** A blog reports a new Seestar firmware photometry mode that writes a per-frame magnitude file. If real and available on the Pro, it makes the first step easier (a cross-check, free) and weakens the case for building a reduction pipeline. If it changes the saved frames, it could also affect point 1. Check what it does before relying on either.

## Odds

These are judgement, not measurements, for this S50 Pro over a reasonable number of clear nights. The same comparison across every science case is in the [science-options note](../first-light/07-science-options.md).

| Outcome | Estimate |
|---|---|
| The saved frames are linear and clean enough for photometry (risk 1) | about 85 to 90 % |
| A slow-variable light curve (Mira, semi-regular, young star, outburst) you could submit to the AAVSO, good to 0.05 mag | about 80 to 85 % |
| ... and AAVSO accepts it without a fight | about 90 % of those |
| An eclipse time of minimum good to 1 to 2 minutes, accepted by a timing database | about 40 to 55 % |
| The autonomous (no human at the telescope) loop working end to end | about 50 to 60 %, the same as transits, mostly the control path |

The earlier transit-only view put a clean, publishable transit near a coin-flip or worse on the S50 Pro. Variable stars are a clear improvement in the odds, and the failure modes that remain are about the unit, which a few nights reveals.

## Sources

* AAVSO Target Tool: `https://targettool.aavso.org/TargetTool/default/index` (parsed 2026-10-29). Documented API (needs a free key): `https://targettool.aavso.org/TargetTool/api/v1/targets`.
* AAVSO, "Examples: doing photometry with the Zwo Seestar S50" (T CrB, 10 x 10 s to about 0.01 mag in `TG`; VPhot and the `TG`/`TB`/`TR` mapping): `https://www.aavso.org/examples-doing-photometry-zwo-seestar-s50`.
* AAVSO, "BVR Transformation Coefficients for Seestar S50" (Bayer order, Tv_bv = 0.020): `https://www.aavso.org/bvr-transformation-coefficients-seestar-s50`.
* Chris Tse, "First Light: Seestar S50's New Photometry Mode on CY Aquarii" (2026-10-14; one blog, unverified against vendor documentation): `https://chris.tsehome.com/first-light-seestar-s50s-new-photometry-mode-on-cy-aquarii/`.
* Chris Tse, "Measuring Supernova SN 2026aaiv with Two Seestars" (S50 and S50 Pro, 2026-09-27/28; one blog): `https://chris.tsehome.com/sn-2026aaiv-measured-with-two-seestars/`.
* Astronomical Society of Edinburgh, "Photometry in ASTAP with a Seestar, monitoring T CrB": `https://www.astronomyedinburgh.org/2024/08/18/photometry-in-astap-with-a-seestar-monitoring-t-crb/`.
* VSX object API: `https://vsx.aavso.org/index.php?view=api.object&ident=Y%20Cyg&format=json` (checked 2026-10-29).
* VSP chart API: `https://apps.aavso.org/vsp/api/chart/?format=json&fov=60&maglimit=14&star=Y%20Cyg` (checked 2026-10-29).
* EccentriCAT-LITE: `https://var3.astro.cz/en/Projects/2` (found earlier; **not re-checked for this note**).
* Pages on `www.aavso.org` were reachable from a browser-like fetch but returned a Cloudflare challenge to a script.
