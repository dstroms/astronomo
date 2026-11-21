"""The plugin contract.

Astronomo core knows *nothing* about asteroids, exoplanets or any other science
case.  A science case is a subclass of :class:`BaseExperimentPlugin` that answers four
questions:

1. *What should we point at?*              -> :meth:`generate_targets`
2. *How should we expose?*                 -> :meth:`get_exposure_plan`
3. *What did the frames tell us?*          -> :meth:`analyze_frames`
4. *Does it match what is known?*          -> :meth:`cross_reference`
5. *What goes into the field's database?*  -> :meth:`build_submissions`
6. *Is anything genuinely anomalous?*      -> :meth:`detect_anomalies`
   (4-6 are optional hooks with safe defaults)

Astronomo takes part in collaborative (Pro-Am) science networks.  So the
question "what should we point at?" is answered from a network's own target list where one exists
(see :mod:`core.networks`), and a mission's product is the upload files from
:meth:`build_submissions` plus a mission log (Markdown, built by core from the mission record, so a
plugin does not write it).  :meth:`detect_anomalies` finds results that reach the significance bar;
they are flagged in the log for a person to review.

Everything a science case needs lives in its plugin package -- including its *simulation*: the
optional :meth:`mock_scene` hook supplies the synthetic sky content (and plugins keep their own
offline stand-ins for services only they use), so shared ``tools/`` never change per experiment.

Contract shapes
---------------
Target (``generate_targets`` -> ``list[dict]``)::

    {
      "name": str,                 # required, unique within the mission
      "ra_deg": float,             # required, ICRS
      "dec_deg": float,            # required, ICRS
      "priority": float,           # optional, higher is observed first
      "window_start": str,         # optional ISO-8601 UTC schedule window
      "window_end": str,           # optional ISO-8601 UTC schedule window
      "not_before": str,           # optional ISO-8601 UTC: do not start this visit before this
                                   # mission-clock time (long-cadence missions, see core/clock.py)
      "designation": str | None,   # optional, plugin-specific
      "network": dict,             # optional: where the target came from and why (core/networks.py:
                                   #   sources, task, reason, score); validated when present
      "sim": dict,                 # optional, simulation truth (ignored by real hardware)
      ...                          # anything else JSON-serialisable
    }

Exposure plan (``get_exposure_plan`` -> ``dict``)::

    {"gain": int, "exposure_s": float, "filter": str, "count": int,
     "interval_s": float   # optional extra delay between frames
    }

Findings (``analyze_frames`` -> ``dict``) -- free-form, but these keys are understood by
the core and dashboard::

    {"summary": str, "detections": list[dict], "figures": list[str], ...}

Data-reduction rule (language models)
-------------------------------------
Plugins reduce data deterministically; a model never sees raw data.  If an LLM is configured the
graph shows it only short digests -- never ``findings`` itself, FITS paths, pixel data or
tables.  Today the one model call (the observing rationale) sees the target list's scalar fields;
:meth:`summarize_for_llm` is the digest a plugin must supply before a model is shown any result.  The default is default-deny (scalars and container sizes only);
override it to expose a few rounded, per-finding numbers.  Whatever it returns must pass
:func:`core.digest.render_payload`, which rejects arrays, long numeric lists and oversize
payloads, so a plugin that forgets to reduce something fails loudly instead of burning tokens.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from config import Settings
from core.clock import parse_time
from core.digest import default_summary
from core.networks import check_network_block

if TYPE_CHECKING:  # annotation only; the contract does not need the simulator at import time
    from core.archive import ArchiveSource
    from core.submission import SubmissionDraft
    from tools.mock_sky import SkyScene


class PluginContractError(ValueError):
    """Raised when a plugin returns data that violates the contract."""


#: What :attr:`BaseExperimentPlugin.status` may be.
PLUGIN_STATUSES = ("ready", "draft")


class BaseExperimentPlugin(ABC):
    """Abstract base class every experiment plugin must implement."""

    #: Human-readable one-liner shown in the dashboard and README listing.
    description: str = ""

    #: ``"ready"`` or ``"draft"`` (:data:`PLUGIN_STATUSES`).  A draft plugin is kept and tested, and runs in
    #: simulation and on archive replays, but is not ready for use: it refuses to command a real telescope unless
    #: ``ASTRONOMO_ALLOW_DRAFT_PLUGINS`` is set, and ``--list-plugins``, the dashboard and the mission log say so.
    status: str = "ready"

    #: Why a draft is a draft, in one sentence (shown beside the label).  Empty for a ready plugin.
    status_note: str = ""

    #: Where this plugin can get frames: ``"telescope"`` always; add ``"archive"`` (and implement
    #: :meth:`archive_source`) to let ``ASTRONOMO_DATA_SOURCE=archive`` replay a public survey.
    data_sources: tuple[str, ...] = ("telescope",)

    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ #
    # Required interface
    # ------------------------------------------------------------------ #
    @property
    @abstractmethod
    def name(self) -> str:
        """String identifier of the experiment (matches the plugin package name)."""

    @abstractmethod
    def generate_targets(self) -> list[dict]:
        """Fetch target coordinates and schedules for this mission."""

    @abstractmethod
    def get_exposure_plan(self) -> dict:
        """Return gain, exposure length (s), filter and frame count."""

    @abstractmethod
    def analyze_frames(self, fits_paths: list[str]) -> dict:
        """Process captured frames and return structured findings."""

    # ------------------------------------------------------------------ #
    # Optional hooks (safe defaults)
    # ------------------------------------------------------------------ #
    def describe_hypothesis(self, targets: list[dict]) -> str:
        """One-paragraph statement of what this mission is testing."""
        return f"Observe {len(targets)} target(s) with the '{self.name}' experiment."

    def cross_reference(self, findings: dict, catalogs: Any, target: dict) -> dict:
        """Compare findings with external catalogs.

        ``catalogs`` is a :class:`tools.catalogs.CatalogClient`.  Return a dict with at
        least ``summary`` (str); plugins typically add ``entries`` (list of per-finding
        dicts) and ``services`` (which catalogs were consulted).
        """
        return {
            "summary": "No cross-referencing implemented by this plugin.",
            "entries": [],
            "services": {},
        }

    def archive_source(self) -> "ArchiveSource":
        """The adapter that supplies frames when ``settings.data_source == "archive"``.

        Archive-capable plugins override this (and list ``"archive"`` in :attr:`data_sources`),
        and make :meth:`generate_targets` return *visits*: one target per survey visit, each with
        ``not_before`` set to when the survey actually looked there.  The graph then waits on the
        mission clock between visits (see ``core/graph.py: wait_for_visit``).
        """
        raise PluginContractError(f"plugin '{self.name}' has no archive data source")

    def target_limit(self) -> int:
        """How many targets one mission may keep (default: ``settings.max_targets``).

        Archive plugins override this: a cap tuned for a handful of telescope fields would
        silently drop most of a survey's visits.
        """
        return max(self.settings.max_targets, 1)

    def mock_scene(self, target: dict) -> "SkyScene | None":
        """Simulation only: what this science case adds to the synthetic sky of ``target``.

        Return a :class:`tools.mock_sky.SkyScene` (a moving object, a variable star, drift ...)
        built from ``target["sim"]``, or ``None`` for a plain star field.  The graph hands it to
        the mock telescope; plugins that call the offline Gaia pass it on (``gaia_cone(scene=)``)
        so catalogue and camera agree.  It is never consulted on real hardware, and it is the
        reason shared tools never need editing for a new science case.
        """
        return None

    def summarize_for_llm(self, findings: dict, crossref: dict | None = None, target: dict | None = None) -> dict:
        """The ONLY view of an observation a language model is allowed to see.

        Return a small JSON-serialisable dict (rounded numbers, a handful of records, no arrays,
        no file paths).  Build it with the helpers in :mod:`core.digest`.  The default keeps only
        scalar values and the *size* of every container -- override to expose more.
        """
        return default_summary(findings, crossref, target)

    def llm_guidance(self) -> str:
        """Optional domain rules appended to the model's system prompt (e.g. "a candidate is
        not a discovery").  Keep it short: it is paid for on every call."""
        return ""

    def opportunities(self, now: Any = None, *, days: int = 30, refresh: bool = False) -> dict | None:
        """What a person could take part in over the next ``days`` days, for the dashboard's Opportunities page.

        Return an *opportunity report* (shape and validator: :mod:`core.opportunities`), or ``None`` if this
        plugin has no network target list to offer.  ``refresh=False`` must not touch the network (read the
        cached lists); ``refresh=True`` asks the networks again.  Never start a mission, move hardware or
        send anything from here: it is a read-only question.  Default: ``None``.
        """
        return None

    def planned_target(self, opportunity: dict, window: dict) -> dict:
        """The target dict for one window of an opportunity, as :meth:`generate_targets` would have produced it.

        A person who plans an observation stores this, so a scheduler can later turn it into a mission without
        asking the plugin again.  The default is the opportunity's base target plus the window; plugins add
        whatever else their ``generate_targets`` puts on a target (a predicted mid-time, for example).
        """
        return {**opportunity["target"], "window_start": window["start"], "window_end": window["end"]}

    def recheck_planned_target(self, target: dict) -> dict:
        """Look again at a planned target just before a mission is built from it.

        A plan was made from the networks' lists on some earlier day; a schedule can move.  Return
        ``{"target": dict | None, "notes": [str]}``: ``target`` is a refreshed copy to use instead of the stored one
        (or ``None`` to keep it), and each note says what changed or could not be checked.  Must not touch hardware
        and should not need the network (read the cached lists).  Default: nothing to re-check.
        """
        return {"target": None, "notes": []}

    def build_submissions(self, mission: dict) -> "list[SubmissionDraft]":
        """Format this mission's results for the databases of the networks it serves.

        This is a mission's *default product*.  Return one :class:`core.submission.SubmissionDraft`
        per file (an AAVSO Exoplanet report, an MPC ADES file ...); ``tools/upload_formats.py`` has
        writers for the standard formats.  ``mission`` holds ``mission_id``, ``plugin_name``, ``hypothesis``,
        ``simulated``, ``targets`` and ``observations`` (one record per target with ``target``,
        ``weather``, ``exposure_plan``, ``findings``, ``crossref``).  Core decides what each file may claim (a simulated or replayed file can
        never be marked uploadable), so list a ``blocker`` for anything a person must fix first (a missing
        observer code, a failed quality gate) and a draft with ``content=None`` for a result you
        are deliberately *not* submitting, with the reason as its blocker.  Nothing is ever sent.

        The default is no files.
        """
        return []

    def detect_anomalies(self, mission: dict) -> list[dict]:
        """Propose anomalies: results that stand far outside what the network's data predicts.

        See :mod:`core.anomaly` for the shape.  Core drops anything below
        ``settings.anomaly_min_sigma``, so be honest about the significance and include every
        candidate; the weak ones are listed in the mission log rather than lost.  An anomaly is
        a lead for a person to review, never a result.  Default: none.
        """
        return []

    # ------------------------------------------------------------------ #
    # Validation helpers used by the graph (not meant to be overridden)
    # ------------------------------------------------------------------ #
    def validated_targets(self) -> list[dict]:
        return self.validate_targets(self.generate_targets())

    def validate_targets(self, raw: Any) -> list[dict]:
        """Check and normalise a list of target dicts (from :meth:`generate_targets`, or a stored plan)."""
        if not isinstance(raw, list):
            raise PluginContractError("generate_targets() must return a list of dicts")
        seen: set[str] = set()
        out: list[dict] = []
        for i, tgt in enumerate(raw):
            if not isinstance(tgt, dict):
                raise PluginContractError(f"target #{i} is not a dict")
            for key in ("name", "ra_deg", "dec_deg"):
                if key not in tgt:
                    raise PluginContractError(f"target #{i} is missing required key '{key}'")
            ra, dec = float(tgt["ra_deg"]), float(tgt["dec_deg"])
            if not (math.isfinite(ra) and math.isfinite(dec)) or not (-90.0 <= dec <= 90.0):
                raise PluginContractError(f"target '{tgt['name']}' has invalid coordinates ({ra}, {dec})")
            name = str(tgt["name"])
            if name in seen:
                raise PluginContractError(f"duplicate target name '{name}'")
            seen.add(name)
            norm = {**tgt, "name": name, "ra_deg": ra % 360.0, "dec_deg": dec}
            if "network" in norm:
                try:
                    check_network_block(name, norm["network"])
                except ValueError as exc:
                    raise PluginContractError(str(exc)) from exc
            if norm.get("not_before"):
                try:
                    norm["not_before"] = parse_time(norm["not_before"]).isoformat()
                except (TypeError, ValueError) as exc:
                    raise PluginContractError(f"target '{name}' has an invalid not_before: {exc}") from exc
            else:
                norm.pop("not_before", None)
            for key in ("window_start", "window_end"):
                if norm.get(key):
                    try:
                        norm[key] = parse_time(norm[key]).isoformat()
                    except (TypeError, ValueError) as exc:
                        raise PluginContractError(f"target '{name}' has an invalid {key}: {exc}") from exc
                else:
                    norm.pop(key, None)
            if norm.get("window_start") and norm.get("window_end") and parse_time(norm["window_end"]) <= parse_time(norm["window_start"]):
                raise PluginContractError(f"target '{name}' has a window_end that is not after its window_start")
            out.append(norm)
        return out

    def validated_exposure_plan(self) -> dict:
        plan = self.get_exposure_plan()
        if not isinstance(plan, dict):
            raise PluginContractError("get_exposure_plan() must return a dict")
        missing = [k for k in ("gain", "exposure_s", "filter", "count") if k not in plan]
        if missing:
            raise PluginContractError(f"exposure plan missing keys: {missing}")
        if plan["exposure_s"] <= 0 or int(plan["count"]) < 1:
            raise PluginContractError("exposure plan needs exposure_s > 0 and count >= 1")
        return {**plan, "count": int(plan["count"]), "exposure_s": float(plan["exposure_s"])}
