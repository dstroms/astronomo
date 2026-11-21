"""Plugin-specific dashboard views.

The dashboard core knows no science case.  A plugin may ship an optional module
``plugins/<name>/dashboard.py`` exposing::

    LABEL = "Exoplanet transit"                       # short mission-type name (optional)
    def summarise(ctx: ViewContext) -> Summary | None # plain-language "what did it find?" (optional)
    def evidence(ctx: ViewContext) -> dict            # JSON for any frontend; Plotly figures as ``plotly`` dicts (optional)

If the module (or a function) is missing, the dashboard falls back to generic behaviour, so a new
plugin works without any UI code.  The label/description are also shown in the mission header, so you
can tell what kind of mission you are looking at.

``summarise`` feeds the "What it found / What it means" block at the top of a mission and the one-line
outcome on the home page.  It must be **deterministic** (computed from the stored findings, never from a
language model) and cheap: it runs on every refresh.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from typing import Any, Callable

VERDICTS = ("good", "neutral", "attention")


@dataclass(frozen=True)
class ViewContext:
    """Read-only data handed to a plugin's ``summarise``/``evidence``.  ``observations`` carry a ``target_name`` key.

    ``simple`` is the dashboard's detail level: ``True`` asks for everyday wording and hides expert-only
    numbers, ``False`` shows everything.  A plugin may ignore it.  ``evidence`` is JSON any frontend can draw.
    """

    mission: dict
    targets: list[dict]
    observations: list[dict]
    simple: bool = False


@dataclass(frozen=True)
class Summary:
    """The plain-language outcome of a mission so far.

    ``found`` is one factual sentence ("2 candidates: both matched known asteroids").  ``meaning`` says
    what that implies, including the honest caveat ("not a discovery").  ``verdict`` colours the
    ``meaning``: ``good`` (as expected), ``attention`` (worth a human look), ``neutral`` (nothing to conclude).
    """

    found: str
    meaning: str
    verdict: str = "neutral"


@dataclass(frozen=True)
class PluginUI:
    name: str
    label: str
    description: str
    summarise: Callable[[ViewContext], Summary | None] | None = None
    evidence: Callable[[ViewContext], dict] | None = None
    status: str = "ready"  # "draft": not ready for use (the mission header says so)
    status_note: str = ""

    def headline(self) -> str:
        """The description shown in a mission's header, led by a draft warning when the plugin is not ready."""
        text = self.description or self.label
        if self.status != "draft":
            return text
        why = f": {self.status_note}" if self.status_note else ""
        return f"DRAFT, not ready for use{why}. {text}"


def _humanise(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def load_plugin_ui(name: str) -> PluginUI:
    """Never raises: a broken or missing plugin module yields the generic fallback."""
    label, description, summarise, evidence = _humanise(name), "", None, None
    status, status_note = "ready", ""
    try:
        module = importlib.import_module(f"plugins.{name}")
        cls = getattr(module, "PLUGIN_CLASS", None)
        description = str(getattr(cls, "description", "") or "")
        status = "draft" if getattr(cls, "status", "ready") == "draft" else "ready"
        status_note = str(getattr(cls, "status_note", "") or "")
    except Exception:  # noqa: BLE001 - e.g. plugin removed or a dependency missing: still show the mission
        pass
    try:
        view: Any = importlib.import_module(f"plugins.{name}.dashboard")
        label = str(getattr(view, "LABEL", label))
        summarise = getattr(view, "summarise", None)
        evidence = getattr(view, "evidence", None)
    except ModuleNotFoundError:
        pass  # no plugin-specific view: generic behaviour only
    except Exception:  # noqa: BLE001 - a bug in a plugin's view must not take the dashboard down
        pass
    return PluginUI(name=name, label=label, description=description, summarise=summarise, evidence=evidence,
                    status=status, status_note=status_note)


def mission_context(db: Any, mission: dict, simple: bool = False) -> ViewContext:
    """Load a mission's targets and observations (each tagged with ``target_name``) for the views."""
    targets = db.list_targets(mission["id"])
    names = {t["id"]: t["name"] for t in targets}
    observations = db.list_observations(mission["id"])
    for obs in observations:
        obs["target_name"] = names.get(obs.get("target_id"))
    return ViewContext(mission=mission, targets=targets, observations=observations, simple=simple)


def summary_for(ui: PluginUI, ctx: ViewContext) -> Summary:
    """The plugin's summary, or a generic one.  Never raises: a summary bug must not hide the mission."""
    # An observation row exists from the moment imaging starts; it only has results once it is processed.
    processed = [o for o in ctx.observations if o.get("findings")]
    if not processed:
        return Summary(
            "Nothing yet: no images have been processed.",
            "Results appear after the first visit has been observed and processed.",
            "neutral",
        )
    ctx = replace(ctx, observations=processed)
    if ui.summarise:
        try:
            out = ui.summarise(ctx)
            if out is not None:
                return out if out.verdict in VERDICTS else Summary(out.found, out.meaning, "neutral")
        except Exception:  # noqa: BLE001
            pass
    n_det = sum(len((o.get("findings") or {}).get("detections", [])) for o in processed)
    n_obs = len(processed)
    return Summary(
        f"{n_det} detection{'' if n_det == 1 else 's'} across {n_obs} observation{'' if n_obs == 1 else 's'}.",
        "This kind of mission has no plain-language summary yet. The Results tab has the details.",
        "neutral",
    )


def figure_json(fig: Any) -> dict | None:
    """A Plotly figure as JSON any frontend can draw.  ``None`` if there is no figure."""
    if fig is None:
        return None
    if hasattr(fig, "to_plotly_json"):
        return fig.to_plotly_json()
    if hasattr(fig, "to_dict"):
        return fig.to_dict()
    return None


def evidence_for(ui: PluginUI, ctx: ViewContext) -> dict:
    """JSON a frontend can draw.  Never raises.  Falls back to a detections table when the plugin has no ``evidence``."""
    if ui.evidence:
        try:
            out = ui.evidence(ctx)
            if isinstance(out, dict):
                return out
        except Exception as exc:  # noqa: BLE001
            return {"error": f"The {ui.label} evidence view failed ({type(exc).__name__}: {exc})."}
    rows = []
    for obs in ctx.observations:
        findings = obs.get("findings") or {}
        xref = {e.get("detection_id"): e for e in (obs.get("crossref") or {}).get("entries", [])}
        for det in findings.get("detections", []):
            row = {"field": obs.get("target_name"), "observation": obs.get("id")}
            row.update({k: v for k, v in det.items() if isinstance(v, (str, int, float, bool)) or v is None})
            row["classification"] = xref.get(det.get("id"), {}).get("classification", "unverified")
            rows.append(row)
    if rows:
        return {"tables": [{"title": "Detections", "rows": rows}]}
    return {"captions": ["No detections yet."] if ctx.observations else ["Nothing yet: no images have been processed."]}
