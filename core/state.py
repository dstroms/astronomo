"""LangGraph state definition.

State is checkpointed to SQLite after every node, so it must only contain
JSON-serialisable values (str / float / int / bool / list / dict / None).  Plugins are
responsible for returning such data; the graph additionally sanitises it with
:func:`to_jsonable` (numpy scalars/arrays, Paths, datetimes ...).
"""

from __future__ import annotations

import math
import operator
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, TypedDict


class MissionStatus(str, Enum):
    INIT = "INIT"
    HYPOTHESIS = "HYPOTHESIS"
    WEATHER_CHECK = "WEATHER_CHECK"
    SLEEP = "SLEEP"
    WAITING = "WAITING"  # between scheduled visits of a long-cadence mission
    OBSERVING = "OBSERVING"
    REDUCING = "REDUCING"
    CROSS_REFERENCING = "CROSS_REFERENCING"
    SCREENING = "SCREENING"  # checking the results for anomalies to flag for a person
    PACKAGING = "PACKAGING"  # formatting the results as upload files for the networks' databases
    WRITING = "WRITING"  # writing the mission log (Markdown)
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"  # an operator stop: the telescope was parked and the mission will not resume


TERMINAL = {
    MissionStatus.COMPLETE.value, MissionStatus.ABORTED.value, MissionStatus.FAILED.value, MissionStatus.STOPPED.value,
}

#: Pipeline nodes in display order (``sleep`` is an off-path node entered from weather;
#: ``wait_for_visit`` is entered only for targets that carry a ``not_before`` time).
NODE_ORDER = [
    "generate_hypothesis",
    "wait_for_visit",
    "check_weather",
    "sleep",
    "release",
    "execute_observation",
    "reduce_data",
    "cross_reference",
    "screen_anomalies",
    "package_submission",
    "write_log",
]

#: Mission status shown while each node is running.
NODE_STATUS = {
    "generate_hypothesis": MissionStatus.HYPOTHESIS.value,
    "wait_for_visit": MissionStatus.WAITING.value,
    "check_weather": MissionStatus.WEATHER_CHECK.value,
    "sleep": MissionStatus.SLEEP.value,
    "release": MissionStatus.SLEEP.value,  # the process has handed the weather wait back; status stays SLEEP
    "execute_observation": MissionStatus.OBSERVING.value,
    "reduce_data": MissionStatus.REDUCING.value,
    "cross_reference": MissionStatus.CROSS_REFERENCING.value,
    "screen_anomalies": MissionStatus.SCREENING.value,
    "package_submission": MissionStatus.PACKAGING.value,
    "write_log": MissionStatus.WRITING.value,
}


class MissionState(TypedDict, total=False):
    # identity
    mission_id: str
    plugin_name: str
    simulated: bool
    data_source: str  # "telescope" | "archive"
    plan_id: int  # a mission built from a planned observation (dashboard Opportunities page) carries its id

    # control
    status: str  # a MissionStatus value
    current_node: str

    # plan
    hypothesis: str
    hypothesis_rationale: str  # optional LLM-written rationale (advisory prose only)
    targets: list[dict]
    target_index: int

    # environment
    weather: dict
    sleep_cycles: int
    deferred_until: str | None  # ISO time the weather wait should be resumed; set when the process is released
    windows_enforced: bool  # targets that carry window_start/window_end are held to them (real telescope, interlocks on)
    target_decision: str  # what the last interlock check decided for the current target: "observe" | "wait" | "skip"

    # per-target working set (overwritten for each target)
    exposure_plan: dict
    observation_id: int
    fits_paths: list[str]
    archive: dict  # provenance of the frames of the current visit (archive missions)
    findings: dict
    crossref: dict

    # accumulated across targets
    observations: Annotated[list[dict], operator.add]
    skipped: Annotated[list[dict], operator.add]  # targets that were not observed, and why: {"name", "reason", "kind"}
    errors: Annotated[list[str], operator.add]

    setup: dict  # first-frame focus / pointing / calibration report, plus the operator's view (core/setup_checks.py)
    autofocus: dict  # what the focus sweep before the first exposure did (tools/autofocus.py); set once per mission

    # output
    anomalies: list[dict]  # reached anomaly_min_sigma (core/anomaly.py)
    below_threshold: list[dict]  # valid but too weak to count; shown in the log
    submissions: list[dict]  # one record per upload file or held-back item (core/submission.py)
    results_dir: str  # the one folder a person opens: upload files, findings summary and mission log
    mission_log: dict  # the LogResult of the automated mission log (writer/markdown_writer.py)
    findings: dict  # the LogResult of the findings summary beside it (core/findings.py)


def initial_state(
    mission_id: str, plugin_name: str, simulated: bool, data_source: str = "telescope", plan_id: int | None = None
) -> MissionState:
    state = MissionState(
        mission_id=mission_id,
        plugin_name=plugin_name,
        simulated=simulated,
        data_source=data_source,
        status=MissionStatus.INIT.value,
        target_index=0,
        sleep_cycles=0,
        targets=[],
        observations=[],
        skipped=[],
        errors=[],
    )
    if plan_id is not None:
        state["plan_id"] = int(plan_id)
    return state


def to_jsonable(obj: Any) -> Any:
    """Recursively convert ``obj`` into plain JSON types (NaN/inf -> None)."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]

    # numpy / astropy scalars and arrays, without importing numpy at module import.
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        try:
            return to_jsonable(tolist())
        except Exception:
            pass
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return to_jsonable(item())
        except Exception:
            pass
    return str(obj)
