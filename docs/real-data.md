# Real data and long cadences

The asteroid plugin can run on **real public data** (ZTF) instead of a telescope. The plugin code is the same either
way; only where the frames come from changes (`--data-source`), and the harness takes care of *when*.

```bash
# ZTF (IRSA): minor-planet visits spread over weeks
python main.py --plugin asteroid_finder --data-source archive --replay-speed 100000
# ...and watch it:
python -m dashboard.api   # http://localhost:8765
```

Archive mode needs the network (the catalogues are the real ones) and never touches a telescope, weather service
or the mock camera. `--simulate` fabricates data and is refused together with `--data-source archive`.

**Survey cadence is part of the science, so it is part of the mission.** A target may carry `not_before` (an ISO UTC
time). Before it is observed the graph runs `wait_for_visit`, which blocks on the *mission clock* until that instant:

| Clock | Meaning |
|---|---|
| telescope mission | wall time |
| `--replay-speed 0` | jump: survey time leaps to the next visit (tests, quick looks) |
| `--replay-speed 1` | real time: a 20-day gap takes 20 days |
| `--replay-speed 3600` | one survey hour per wall second: a 20-day gap takes 8 minutes |

Frames within a visit are also released at their real spacing on that clock (so a burst of frames arrives at its real
cadence, and the dashboard shows them arriving), and the visits are processed in time order whatever their priority.
The clock state is saved with the mission; a resumed mission keeps its own clock whatever speed you pass.

* **ZTF** is bursty and irregular, not periodic: a field is revisited in a burst of a few exposures, then again
  hours or days later, and a field can be seen by more than one CCD or filter. The adapter groups exposures into
  visits (same field/CCD/quadrant/filter, gaps under 6 h), keeps visits with at least 3 frames (fewer cannot show
  motion), and reports the cadence it found (one row per visit in the mission log). Replayed data is credited in the mission log, and its
  upload files are `REPLAY_` files that can never be marked ready.

**Resumable.** The graph is checkpointed, so a mission that dies mid-wait or mid-visit restarts from the node it
was in (an interrupted acquisition is repeated; its half-finished record is discarded). `python main.py --plugin P
--mission-id ID` resumes a specific one; without `--new`, `main.py` resumes the newest unfinished mission that no
live process owns.

**Several missions at once = several processes.** Each running mission is *leased*: its process stamps a heartbeat
into the database every few seconds. Another `main.py` will not pick up a mission with a fresh heartbeat; one with
a stale heartbeat (killed, crashed, machine slept) is *orphaned* and is taken over on resume; a mission whose
process let go part-way (Ctrl-C) is *paused*. The dashboard's **Home** page shows every mission together, grouped
**Needs attention** (orphaned, paused, or failed in the last 24 h, with the command to resume), **In progress** and
**Completed**: state in plain words, whether a process still owns it, visit *i* of *n* and frames done/total, when the
next visit is due (in survey time and in real time at the replay speed), and a one-line outcome. The sidebar lists
the same missions as a navigation rail, so any mission is one click away; **Open** jumps to its page.

What is *not* done: there is no daemon that wakes sleeping missions. A waiting mission is a blocked process, which
is simple and robust to crashes (see above) but holds a process and a lease for the whole replay.

## The live Pro-Am clients, checked against the real services

| Service | Result | Fixture |
|---|---|---|
| Exoplanet Watch | 3030 of 3030 rows parse and agree with the literature (WASP-12 b P = 1.0914 d; HD 189733 b depth 2.4 %, V 7.67). Blank epoch uncertainty (4 rows, read as 0) and blank period uncertainty (2 rows, treated as an unknown staleness) are handled; the `host` column is empty in every row | `tests/data/exoplanet_watch_real.csv` (43 rows) |
| NASA TTV flag | 497 names | `tests/data/nasa_ttv_flag_real.csv` |
| MPC NEOCP | 29 of 29 rows; RA is in hours and the parser reads it that way | `tests/data/neocp_real.json` (8 rows) |
| ExoClock | Fetched live on 2026-11-14. 776 of 776 planets parsed. WASP-12 b's period is 1.09142 d and its position matches the Exoplanet Watch row; WASP-2 b's duration is 1.77 h. An O-C is kept only when `total_observations_recent` is above 0: 299 planets have none, and 255 of those publish 0.0, which is read as no measurement. Priorities in the file: alert 41, high 345, medium 139, low 251. No row was skipped | `tests/data/exoclock_planets_real.json` (7 planets) |

The run found one bug: an error page returned with HTTP 200 was cached as if it were the data, so a later run would have
used it for 24 hours. `fetch_text(..., validate=...)` now checks the body before it is cached, and a body that fails the
check is treated like a failed download. The tests that use these fixtures never touch the network.
