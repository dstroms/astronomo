# 1. AAVSO observer code, site and equipment

**Why:** every AAVSO Exoplanet Report carries your observer code, and NASA's Exoplanet Watch ingests transit
light curves from the AAVSO Exoplanet Database. Until `ASTRONOMO_AAVSO_OBS_CODE` is set, every Astronomo upload file is
a `DRAFT_` by design. It costs nothing.

**You do this yourself. Astronomo does nothing on these sites.**

**Status (2026-11-14):** done. The observer code was assigned on 2026-10-28 and is set in the local `.env` as `ASTRONOMO_AAVSO_OBS_CODE`. It is not written in this repository. The observing site and the Seestar equipment package are in the AAVSO account, so the upload form can choose them. An upload still needs a finished report and a finder image.

## Steps

1. **Create an AAVSO account.** Go to <https://auth.aavso.org/u/login>, choose *Sign up*, and confirm the email they send (check spam).
2. **Check you do not already have a code.** AAVSO codes are permanent. If you ever submitted data to the AAVSO, even decades ago, do **not** request a new one: email `aavso@aavso.org` and ask to recover it. A draft is below.
3. **Request the code.** Log in, open *Account Settings* (the account menu at the top of any page), scroll to *Observer Settings*, and click **Request Obscode**. Confirm. The code (up to 5 characters) is assigned automatically.
   Source: <https://aavso.org/register-as-an-observer/>
4. **Add your site.** In the same account area, *Observing Sites*:
   * Name: something descriptive, for example `Home, <town>` .
   * Latitude, longitude, altitude of **where the telescope stands**, not where you live if they differ. Use the same figures as your Astronomo site (`ASTRONOMO_SITE_LAT`, `_SITE_LON`, `_SITE_ELEV_M`). Longitude is **west-negative** in the AAVSO form.
5. **Add your equipment.** *Observing Equipment*, one package per configuration. The exoplanet upload needs the section titled "Required for submitting exoplanet data" to be filled in. Suggested values for a Seestar S50 (confirm each against ZWO's current specification sheet before entering it):

   | Field | Value |
   |---|---|
   | Observing type | CCD (the AAVSO says to code a CMOS camera as CCD) |
   | Telescope aperture | 50 mm |
   | Focal length | 250 mm |
   | Camera | Sony IMX462 colour CMOS (inside the Seestar) |
   | Detector pixel count, width x height | 1920 x 1080 |
   | Pixel width and height | 2.9 microns |
   | Filters | `CV` (clear; the Seestar's IR-cut is not a photometric filter) |

   Say in the notes that it is a colour (Bayer) sensor and which channel or sum Astronomo measures; the exoplanet report's `#NOTES=` line repeats this for each file.
6. **Put the code in Astronomo.** In `.env`: `ASTRONOMO_AAVSO_OBS_CODE=<your code>`. Restart the dashboard. (`ASTRONOMO_OBSERVER_NAME` is only used by the MPC asteroid file, not the AAVSO ones.)

## Where Astronomo uses the code

* It is written into the `#OBSCODE=` header line of the AAVSO files Astronomo prepares: the Exoplanet Report from `transit_finder` and the Extended report from `rubin_followup`.
* With it unset, the file is written with a placeholder and named `DRAFT_`, with "no AAVSO observer code" listed as a blocker in `manifest.json`. Setting it removes that blocker only; other blockers (for example an unconfirmed host star) can remain.
* `SIMULATED_` and `REPLAY_` files stay blocked whatever you set.
* It is not a login and is not used to choose targets. Astronomo never contacts the AAVSO and never sends a file (`sent` is always `false`); you upload it yourself.

## What an upload looks like later (for reference, not now)

Astronomo writes `<planet>_<night>_AAVSO_Exoplanet.txt`. To upload it: log in, open the **Exoplanet Database "Submit Observation"** page (<https://apps.aavso.org/exosite/submit>), choose the site and the equipment package, attach the report, and attach **at least one plate-solved image showing the target and the comparison stars** (at most 10 files, under 2 MB in total). Tick the GDPR box and submit.

**Known gap:** Astronomo attaches the light-curve figure, not a plate-solved finder image with the comparison stars marked. The AAVSO asks for the latter. Until Astronomo makes one, annotate a frame yourself (for example with AstroImageJ or Astrometry.net's annotation).

First contribution advice from the AAVSO: pick a well-known transit, for example HD 209458 b (<https://wp-live.aavso.org/contributing-data/>). That is also the right kind of first-light target.

## Draft: recovering an existing observer code (send only if it applies)

> **To:** aavso@aavso.org
> **Subject:** Recovering my AAVSO observer code
>
> Hello,
>
> I would like to start submitting exoplanet transit photometry and I believe I may already have an observer code from past participation (or I am not sure). My details:
>
> Name: [YOUR NAME]
> Email I would have used: [EMAIL]
> Approximate year and kind of past activity: [E.G. VISUAL OBSERVATIONS, 20XX]
>
> Could you tell me whether a code exists for me so that I reuse it rather than request a new one?
>
> Thank you,
> [YOUR NAME]

## Sources

* <https://aavso.org/register-as-an-observer/>
* <https://www.aavso.org/new-observers>
* <https://apps.aavso.org/exosite/doc>
* <https://science.nasa.gov/citizen-science/exoplanet-watch/how-to-contribute/how-to-submit-your-data/>
