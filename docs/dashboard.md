# Dashboard design guide

How the dashboard looks and why, in one page. The code is the source of truth: the colours and type are in [`frontend/src/theme.css`](../frontend/src/theme.css) and the pages are in [`frontend/src/pages/`](../frontend/src/pages/). This page records the rules behind them, so a change (or a plugin's own view) follows the same pattern. The HTTP contract is [api.md](api.md). A plugin's own view is described in [writing a plugin](writing-a-plugin.md#plugin-specific-dashboard-view-optional): the plugin returns a summary and JSON (Plotly figures through `figure_json`), and the dashboard draws it, so a plugin author does not write page code.

The dashboard was redesigned in 2026-11 and moved from Streamlit to React. The long design package behind that (an audit, wireframes, an implementation plan) was removed on 2026-11-20 because it described the Streamlit app that no longer exists. It is in the git history before that date if you need it.

## The pages

A night is lived in this order: what needs me now, what is next, what could I do. One top bar, four pages:

| Page          | Route            | What it is for                                                                                         |
| ------------- | ---------------- | ------------------------------------------------------------------------------------------------------ |
| Tonight       | `/`              | Operating: what needs a person, what is running or waiting, the live plans (Rehearse, Start), files ready to review. The home page. |
| Opportunities | `/opportunities` | Deciding: read-only lists and filters, then "Plan this". It starts nothing.                             |
| Missions      | `/missions`, `/missions/:id` | Reviewing: every mission in one table with what it produced. A mission is a state of this page, not a page in the bar. |
| Settings      | `/settings`      | The observing site, the data directory, refresh. Not somewhere anyone goes mid-mission.                 |

Rules that hold across pages:

- **A count on a nav item means "a person is needed", and only that.**
- **A control that navigates is a link; a control that changes something is a button.**
- **Everything worth bookmarking is in the URL.**
- **The real-hardware start is a two-step action** (Start, then Confirm with the printed command) and is never a primary button. It must not look like Rehearse.
- **Nothing sends anything.** A file is shown as ready for review, and a person sends it.

## Principles

1. **Quiet by default, loud by exception.** Only things that need a person get colour or weight. *Complete* is a small green dot, not a filled green block.
2. **Answer first, evidence on demand.** The conclusion is the largest text on a card, the facts sit in a strip, and the explanation is behind a disclosure.
3. **Night-safe.** The dashboard is used beside a telescope with dark-adapted eyes. Dark is the primary theme: no pure white, no large saturated fills, nothing brighter than the text. A light theme exists for planning in daylight.
4. **One meaning per colour, and colour is never alone.** Every status also has a word, and the main states also differ by dot shape (filled or hollow).
5. **Same thing, same look.** One badge, one fact strip, one card anatomy, one chart style.
6. **Scientific, not trendy.** 2 to 3 px corners, 1 px borders, no shadows, no gradients, no emoji, sentence case.

It is deliberately not a code-hosting look: a neutral graphite ground (not navy), a steel blue accent (not saturated azure), square-ish geometry, and tabular figures so columns of numbers line up like an instrument readout.

## Colour

The palette is in `theme.css`. The contrast of every text-on-background pair was computed when it was chosen (the smallest text contrast is 4.8:1; WCAG AA is 4.5:1). Do not change a value without recomputing it.

| Slot     | Night     | Day       | Meaning (one only)                                         |
| -------- | --------- | --------- | ---------------------------------------------------------- |
| `blue`   | `#5BB9E8` | `#17658F` | **Active** or informational: working now, selected, planned |
| `green`  | `#4DBB8A` | `#17724F` | **Done** and healthy: complete, clear sky, high value       |
| `orange` | `#E48A4B` | `#A84A14` | **Needs a person**: interrupted, paused                     |
| `red`    | `#F07676` | `#B3261E` | **Failed or unsafe**. Never a selected or primary state     |
| `yellow` | `#E0B04A` | `#7F5700` | **Caution inside content**: a check warning, a verdict that needs a look |
| `violet` | `#A58BE8` | `#6542B8` | **Not live sky**: simulated or archive-replay data          |
| `gray`   | `#99A1AB` | `#5A626C` | **Neutral or inactive**: stopped, not started, little gain  |

- **Orange or yellow:** orange is a lifecycle signal (a mission needs a person), yellow is a warning inside content. Never use both for one thing.
- **Violet marks "this is not the real sky"** everywhere a simulated or replayed mission appears, because mistaking a rehearsal for a real run is the costly error.
- **Green is rare by design:** a small dot, a badge or a progress fill, never a large block.
- **No colour values in code.** Name a slot (`"green"`) and let the theme decide, except where a convention is read by colour (the Rubin band colours). This is the rule, not yet the state of the code: `dashboard/vocab.py` still holds hex values for the mission states, and moving them to slot names is open.

**Mission states** share six looks, and the state's label always accompanies the look:

| Look      | Colour and dot    | States                                                                      |
| --------- | ----------------- | --------------------------------------------------------------------------- |
| Active    | blue, filled      | `HYPOTHESIS` `WEATHER_CHECK` `OBSERVING` `REDUCING` `CROSS_REFERENCING` `SCREENING` `PACKAGING` `WRITING` |
| Waiting   | blue, hollow      | `WAITING` `SLEEP` (alive, idle on purpose)                                  |
| Attention | orange, filled    | `ORPHANED` `PAUSED`                                                         |
| Failed    | red, filled       | `FAILED`                                                                    |
| Done      | green, filled     | `COMPLETE`                                                                  |
| Inactive  | gray, filled or hollow | `ABORTED` `STOPPED` (ended without finishing); `INIT` `IDLE` (not started) |

Other vocabularies use the same slots: plan status (`PLANNED` blue, `DONE` green, `MISSED` and `CANCELLED` gray), opportunity value (`high` green, `medium` blue, `low` and unknown gray), setup checks (`PASS` green, `WARN` yellow, `FAIL` red) and mission verdicts (good green, attention yellow, neutral blue). The `WRITING` state is labelled "Writing the mission log".

## Type, shape and charts

- **Type:** IBM Plex Sans for text and IBM Plex Mono for identifiers, commands and coordinates, falling back to system fonts. They are loaded from Google Fonts (`frontend/index.html`), so with no internet at the telescope the dashboard quietly uses the system fonts; bundling the font files would fix that and is open. Root size 14 px, sentence case, one title per page.
- **Shape:** 2 px corners on controls and tags, 3 px on cards and tables, 1 px borders, no shadows. Tags are flat tinted rectangles with no outline.
- **No emoji anywhere.** The brand is a small reticle mark: a ring, a centre dot and four ticks.
- **Charts** share one style: transparent background, horizontal grid only, 1 px axes, no mode bar, nothing taller than about 360 px. **Colour by role, not by chart:** measured data gray, a model or fit blue, a catalogue or expected value green, "now" an orange marker, a threshold a dotted gray line, and red only for a failure.

## Decisions that were made

These came from the redesign and still hold:

1. The home page is **Tonight**, and it absorbs Plans (a plan is a section of Tonight, not a page).
2. Opportunities is **compact rows plus the selected card**, not a stack of cards. "Kind of observation" is a checklist: several kinds can be shown at once in one merged list.
3. A mission page leads with the result and its deliverables, with tabs by depth (Summary, Evidence, Documents, Process).
4. The mission page has no sidebar.
5. Two read-only views were added: Ready for review on Tonight and a Nights view on Opportunities. Neither starts or writes anything.
