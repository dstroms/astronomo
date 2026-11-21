# MPC observatory code: read this before you plan on one

**Why it matters:** `ASTRONOMO_MPC_STATION_CODE` turns an asteroid ADES file from a `DRAFT_` into a file the MPC will
accept. Without a code, you cannot submit astrometry from your own scope.

**The requirement is steep for a 50 mm telescope.** From the MPC's current rules
(<https://docs.minorplanetcenter.net/mpc-ops-docs/observatory-and-program-codes/observatory-codes/>, checked 2026-10-28):

* **At least ten numbered Near-Earth Asteroids**, no comets, satellites or unnumbered objects.
* **Every object fainter than magnitude 16.**
* Every object observed on **two distinct nights**; **3 to 5 measurements per object per night**; at least one **magnitude** per object per night.
* Submitted as **ADES PSV or ADES XML** (not the old format), using the **placeholder code `XXX`** in the header and every line, with a header comment such as `Long. 123 45 67.8 E, Lat. 12 34 56.7 N, Alt. 123m, Google Earth`.
* Poor residuals (position, time, magnitude) mean no code is assigned and you must submit better measurements.

### Can a Seestar S50 do this?

**Unknown, and possibly not on a normal night.** Reports from other owners disagree:

* A Seestar owner reports a limiting magnitude of about 14.6 in a single 10 s frame, and about 16 only after stacking a dozen or more frames (Cloudy Nights thread on near-real-time observation).
* Another reports a mag-16 asteroid measured from 36 x 10 s stacked, with astrometry within about 0.1 arcsec of JPL Horizons (Cloudy Nights, "(2740) TSOJ, mag 16, 2-inch scope").
* An AstroBin thread reports easily following asteroids to about mag 14.5 to 15.

Fainter than 16 means working at or past the limit, in dark and clear skies, on **fast-moving** NEAs that smear during a stack. Treat the code as **a stretch goal, not a first-light item**. The honest plan is:

1. Do the transit work first (AAVSO needs only a code and no magnitude limit like this).
2. Once you have real frames, measure what magnitude your scope reaches at your site, in one stack (row 13 of the [open hardware questions](../hardware-questions.md) records it).
3. Only then decide. If you are 1 to 2 magnitudes short, alternatives are: a collaborator who already holds a code, a remote telescope with a larger aperture for the qualifying astrometry, or leaving MPC astrometry out of scope.

**Astronomo's current gaps for this route** (so you know what is missing before you try): it does not write the `XXX` qualifying file or the header comment, and it measures single frames or its own stacks without a check that each object is a numbered NEA fainter than 16. Those would need building.

## If you decide to apply: the steps (all by you)

The order matters. Do both **on the same day, with the same email address**.

1. Choose ten numbered NEAs fainter than 16 that are visible from your site. The MPC publishes an *Observing Target List* for this.
2. Observe each on two nights (3 to 5 positions each night, with magnitudes). Do **not** submit partial data.
3. Prepare one ADES file (PSV or XML) with station `XXX` and the coordinate comment line in the header. Read the ADES submission page first: <https://minorplanetcenter.net/iau/info/Astrometry.html>.
4. Submit the ADES file with the **ADES Submission Form** (or the cURL method; ADES cannot be sent by email). You will get an **acknowledgement email with a submission ID**. If you get nothing, the file was probably rejected for a format error; check the "Where Are My Observations" (WAMO) page.
5. Fill in the **New Observatory Code Request Form**: <https://minorplanetcenter.net/new_obscode_request>. You need:
   * contact name and the **same email address** as the submission, and the **submission ID**;
   * observatory name (optional, must not contain the name of a living person), nearest town or feature, country;
   * latitude and longitude in WGS-84 as **degrees, minutes and seconds with a decimal on the seconds** (not decimal degrees; no negative longitudes: give W, or the 0 to 360 east form), precise to 0.1 arcsec or better, with the source (Google Earth is what the MPC uses to check);
   * altitude in metres above mean sea level, and the height of the telescope above ground (0 if the altitude is the telescope's);
   * the source of coordinates (for GPS, also the reference frame: EGM-96 is mean sea level).
6. You will receive two emails. Keep the one titled `MPCHLP-nnnn New obscode request by ...`; replies to it go to the MPC. Codes are assigned weekly in batches; if more than two weeks pass, reply to that email.
7. When the code arrives, put it in `.env` as `ASTRONOMO_MPC_STATION_CODE` and **stop using `XXX`**.

Coordinates you can reuse: the form wants the *same* figures as your [AAVSO site](../runbooks/aavso-observer-code.md); get them once, precisely, and copy them.

## Draft: query to the MPC (only if you are unsure the telescope qualifies)

The MPC asks you to reply to the MPCHLP thread after you have a ticket; before that, the documentation hub is the place to read. If you still want to ask
about a small instrument before spending nights on it, use the MPC's help desk linked from <https://docs.minorplanetcenter.net/>:

> **Subject:** Observatory code for a 50 mm smart telescope: is the qualifying sample realistic?
>
> Hello,
>
> I plan to apply for an observatory code with a 50 mm aperture smart telescope (ZWO Seestar S50, 10 s exposures, stacked in software; roughly 1.2 to 2.4 arcsec per pixel). I understand the requirement is ten numbered NEAs fainter than magnitude 16, each on two nights. Before I spend nights on it, do you have advice on whether such an instrument can realistically meet it, or on whether stacking several short frames counts as one measurement for the 3 to 5 per night?
>
> Thank you,
> [YOUR NAME]
