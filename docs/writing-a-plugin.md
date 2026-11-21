# Writing a plugin

A plugin is a sub-package of `plugins/` whose `__init__.py` exposes `PLUGIN_CLASS`:

```python
# plugins/my_experiment/__init__.py
from plugins.my_experiment.plugin import MyExperiment
PLUGIN_CLASS = MyExperiment

# plugins/my_experiment/plugin.py
from core.base_plugin import BaseExperimentPlugin

class MyExperiment(BaseExperimentPlugin):
    name = "my_experiment"             # must equal the package name
    # status = "draft"                 # optional: "ready" (the default) or "draft", see below

    def generate_targets(self) -> list[dict]:
        return [{"name": "M13", "ra_deg": 250.42, "dec_deg": 36.46, "priority": 1.0}]

    def get_exposure_plan(self) -> dict:
        return {"gain": 80, "exposure_s": 10.0, "filter": "IRCUT", "count": 60}

    def analyze_frames(self, fits_paths: list[str]) -> dict:
        ...                            # your reduction
        return {"summary": "Found 3 things", "detections": [], "figures": []}
```

Then `ASTRONOMO_PLUGIN=my_experiment python main.py` (or `--plugin my_experiment`). `plugins/transit_finder/`
is a complete, tested example with a long observation, its own simulated sky and an ephemeris cross-check.
Optional hooks with safe defaults: `build_submissions`, `detect_anomalies`, and `result_notes` with `caveats` (see below), `mock_scene` (what your experiment adds to the simulated sky, see below), `describe_hypothesis`, `cross_reference`, and, if you want a language model to comment on your
results, `summarize_for_llm` / `llm_guidance` (see [Language models](models.md#language-models-optional)). The full contract, with the dict shapes, is the module
docstring of `core/base_plugin.py`. `tests/test_graph.py::test_core_runs_an_unrelated_plugin_with_default_hooks`
runs a non-asteroid toy plugin through the whole graph, which is the proof the core is agnostic.

**Draft plugins.** A plugin that is not ready for use sets `status = "draft"` (and a one-sentence `status_note` saying why). It
keeps working in simulation and on archive replays, but a real-telescope run with it is refused unless the person sets
`ASTRONOMO_ALLOW_DRAFT_PLUGINS=1`, and `--list-plugins`, the dashboard's mission header and the mission log all say "DRAFT". Use it for
a plugin that has not yet been seen on the sky.

## Taking part in a network (targets, upload files, anomalies)

Astronomo serves collaborative networks. Three hooks carry that, all optional with safe
defaults, and none of them touches `core/` or `tools/`:

**Observing windows.** A target may carry `window_start` and `window_end` (ISO times). On a real telescope the mission waits for the window to
open and skips the target if it has closed (`core/interlocks.py`); in simulation they are recorded, not enforced. Give a window only when the
science needs one (a transit has one; a field that can be observed any night does not), because a target that outlives its window is skipped.

**Targets with a network label.** Add a `network` block to each target so the mission log can say who asked for it and why:

```python
from core.networks import network_block, TASK_EPHEMERIS

target["network"] = network_block(["exoclock"], TASK_EPHEMERIS, "Its predicted transit time is uncertain by +/-40 min.", score=2.9)
```

Tasks are `ephemeris_refinement`, `ttv_monitoring`, `astrometry_followup`, `transient_followup` and `manual`. A malformed block is a
`PluginContractError`. Fetch lists with `core.networks.fetch_text(url, cache_path, ttl_s=...)` (cached, stale-tolerant, raises
`NetworkError`), keep the parsing in your package (`plugins/transit_finder/networks.py` and `plugins/asteroid_finder/mpc.py` are
examples), and **fall back visibly**: if the network is unreachable, use a short built-in list tagged `manual`. Tests must not
touch the network: the shared test fixtures make `core.networks._http_get_text` raise, and a test that wants content patches it.

**Upload files.** `build_submissions(mission)` returns a list of `core.submission.SubmissionDraft`, one per file, built with the
writers in `tools/upload_formats.py` (`aavso_exoplanet_report`, `aavso_extended_report`, `ades_psv`; each raises `FormatError` for
input its format cannot hold). Put anything a person must fix first in `blockers`, things a reviewer should know in `notes`, and
return `content=None` (with the reason as a blocker) for a result you are deliberately not submitting. Core sets the status,
prefixes the file name, writes `manifest.json` and refuses to call a simulated or replayed file ready. Nothing is ever sent.

**Opportunities (optional).** If your plugin has a network list, `opportunities(now, days=30, refresh=False)` lets the dashboard's
Opportunities page show what a person could take part in before a mission starts. Return a report in the shape of
`core/opportunities.py` (each opportunity says whether it is reachable and, if not, why in a sentence; reachable ones carry windows and
an impact estimate that **lists its assumptions**), and run it through `check_report` before returning. You can add an optional
`impact_guide` (a title, a summary and sections of text) that the page shows in a modal from each estimate's assumptions box; use it to say
what an estimate in your science case is and is not worth. `refresh=False` must read cached
lists and never touch the network. Override `planned_target(opportunity, window)` to give the target dict your `generate_targets`
would produce for a window, and `recheck_planned_target(target)` to look again at a stored plan just before a mission is built from it
(return `{"target": refreshed or None, "notes": [...]}`; read cached data only). The page knows no science case beyond two
conventions it uses to label a window. A positive `duration_h` in `facts`, with a window `mid`, is the length of an event
centred on that mid; the window's own start and end are the proposed mission, and the page shows both, with the mission's
length. A report whose `words` use `chance` of `night`, or a planned target with `window_kind` of `night`, is a usable
stretch of night and is not given an event. When more than one plugin returns a report, the page offers a switch between
them (your report's `label`, and an optional one-sentence `summary`); a single report is shown on its own. See
`plugins/transit_finder/opportunities.py`.

**Anomalies.** `detect_anomalies(mission)` returns dicts `{"kind", "target", "summary", "significance_sigma", "evidence"}`. Core keeps
only those at or above `ANOMALY_MIN_SIGMA` (the rest appear in the mission log as below threshold), so report honest, finite
significances, include a systematic floor where there is one, and give a measurement you cannot trust (a wrong star, say) a
significance of 0 with the measured value in `evidence`. An accepted anomaly is flagged in the mission log for a person to review; nothing is written up.

**What the numbers mean.** Core writes a findings summary beside the upload files, and only your plugin knows what its numbers mean.
`result_notes(observation)` returns plain strings for one observation: `measured` (the result with its units), `error` (the error you
quoted and what it includes and leaves out) and `compared` (how it compares with what is published, or that nothing was available).
`caveats()` returns the standing sentences a person could get wrong (which scale a magnitude is on, what the error leaves out, that a
lead is not a discovery). The default `caveats` admits that the plugin has not said, and `tests/test_findings.py` fails for a shipped
plugin that stays silent. Do not round an error down, and say so when none is quoted.

## Supporting real data (optional)

Set `data_sources = ("telescope", "archive")` and implement `archive_source()` returning a
`core.archive.ArchiveSource` (`acquire(target, out_dir, on_frame) -> FrameBatch`). Put each visit's UTC start in
the target's `not_before`; the harness does the waiting. `on_frame(i, total, obs_time)` must be called before each
frame is produced so it can be paced on the replay clock. Frames must carry `DATE-OBS`, `EXPTIME` and a WCS.
Loading a plugin with a data source it does not list fails with a clear error.

## Plugin-specific dashboard view (optional)

Astronomo is headless: a mission does not need a UI. The bundled React client and any other frontend
read `/api/v1` and show each mission's type (the plugin's `LABEL`) and its `description`. A mission
page opens with an **answer block**: what it is, where it is, what it found, and what that means,
then Overview, Frames, Results, Report and Activity. The **Simple | Technical** switch changes the
wording and hides expert-only numbers; the data is the same.

To give your plugin its own Results view and its own "what it found" sentence, add
`plugins/my_experiment/dashboard.py`:

```python
from dashboard.plugin_ui import Summary, ViewContext, figure_json

LABEL = "My experiment"

def summarise(ctx: ViewContext) -> Summary | None:        # only called once at least one observation is processed
    n = len(ctx.observations)
    return Summary(
        found=f"{n} field(s) observed.",                   # one factual sentence
        meaning="Nothing unusual.",                        # what it implies, with the honest caveat
        verdict="neutral",                                 # "good" | "neutral" | "attention"
    )

def evidence(ctx: ViewContext) -> dict:   # JSON for any frontend (Plotly figures via figure_json)
    return {"captions": [f"{len(ctx.observations)} field(s) observed."]}
```

`summarise` must be deterministic (computed from the stored findings, never from a language model) and cheap, because
it runs on every refresh; it also supplies the one-line outcome on Tonight. `evidence` is what the HTTP API
returns for Results (tables, captions, metrics, and Plotly JSON). Both are optional. Without them the
dashboard shows the generic Detections table and a generic "N detections across M observations" summary.
A view that raises shows an error message and does not affect the rest of the page. Use
`dashboard.vocab.GLOSSARY` for `help=` text on terms a newcomer will not know.
See `plugins/asteroid_finder/dashboard.py` (tracks) and `plugins/transit_finder/dashboard.py` (light curve and fit).

A mission page is `/missions/{id}`.

## Simulating your experiment (no changes to `tools/`)

`python main.py --simulate` needs images and catalogue answers that agree with each other. The generic part
(star field, noise, hot pixels, WCS, pointing error) is `tools/mock_sky.MockSky`; whatever is specific to *your*
experiment is a `SkyScene` you return from `mock_scene(target)`, built from `target["sim"]`:

```python
from tools.mock_sky import SkyScene, counts_for_mag

class MyScene(SkyScene):
    def draw(self, sky, signal, ctx):            # add sources to every frame (ADU, before noise)
        sky.stamp(signal, 960 + ctx.offset_px[0], 540 + ctx.offset_px[1], counts_for_mag(12.0, ctx.exposure_s))
    def catalog_stars(self, sky):                # real stars the offline Gaia should also know about
        return [(960.0, 540.0, 12.0)]
    # optional: pointing_offset_px(sky, when) for drift, transparency(sky, when, frame_index) for clouds

class MyExperiment(BaseExperimentPlugin):
    def mock_scene(self, target):
        return MyScene()
```

The graph hands the scene to the mock telescope (`capture_sequence(..., scene=)`); in `cross_reference`, pass it on to
the offline Gaia (`catalogs.gaia_cone(..., target=target, scene=self.mock_scene(target))`) so camera and catalogue
agree. A service only your plugin uses (an ephemeris, an archive) keeps its offline stand-in in your package
too: see `plugins/asteroid_finder/scene.py` (`AsteroidScene.ephemeris`, the offline Horizons) and
`plugins/transit_finder/archive.py`. Scenes are ignored on real hardware.
