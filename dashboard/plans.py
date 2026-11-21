"""Saved plans and the hand-off that starts one.

A plan is a note until a person starts it.  Rehearse and Start both run ``python main.py --from-plan``,
the same hand-off as the terminal: the plan is re-checked there, a closed window is refused, a rehearsal
leaves the plan as it was, and a real start is a separate confirmation.  Nothing here talks to the
telescope, and nothing starts a plan on its own.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.lease import liveness
from db.database import LIVE_PLAN_STATUSES
from dashboard.fleet import fmt_span

ROOT = Path(__file__).resolve().parent.parent

#: Shown first, then by window.  Cancelled plans are not in this list.
_STATUS_ORDER = {"STARTED": 0, "PLANNED": 1, "DONE": 2, "MISSED": 3}
_OPENABLE = ("PLANNED", "STARTED")
_LOG_TAIL = 12


class LaunchError(Exception):
    """A plan was not started (one process for it is already running)."""


@dataclass(frozen=True)
class LaunchSpec:
    """How to run the hand-off for one plan.  ``command`` is the shell form shown to a person."""

    plan_id: int
    rehearse: bool
    argv: list[str]
    env: dict[str, str]
    command: str


def ordered_plans(plans: list[dict]) -> list[dict]:
    """Non-cancelled plans, in-progress first, then soonest window."""
    live = [p for p in plans if p.get("status") in _STATUS_ORDER]
    return sorted(live, key=lambda p: (_STATUS_ORDER[p["status"]], p.get("window_start") or "", p.get("id") or 0))


def load_plans(db, now: datetime | None = None) -> list[dict]:
    """Expire windows that closed unstarted, then every plan a person might still look at."""
    db.expire_planned(now or datetime.now(timezone.utc))
    return ordered_plans(db.list_planned(status=LIVE_PLAN_STATUSES))


def plan_actions(status: str, *, mission_running: bool) -> dict[str, Any]:
    """Which buttons a row offers.  A mission that is already running is left to its own process."""
    open_ = status in _OPENABLE and not mission_running
    return {
        "rehearse": open_,
        "start": open_,
        "start_word": "Resume" if status == "STARTED" else "Start",
        "again": status in ("DONE", "MISSED") and not mission_running,
    }


def _shell_assign(key: str, value: str) -> str:
    if any(ch in value for ch in " \t\"'$"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f"{key}={value}"


def _data_prefix(data_dir: Path, default_data_dir: Path) -> list[str]:
    if Path(data_dir).resolve() == Path(default_data_dir).resolve():
        return []
    return [_shell_assign("ASTRONOMO_DATA_DIR", str(data_dir))]


def launch_spec(plan_id: int, *, rehearse: bool, data_dir: Path, default_data_dir: Path) -> LaunchSpec:
    """The process the button starts.

    Rehearse is ``--simulate --fast``: the simulated sky, and the plan stays as it was.  A real start sets
    ``ASTRONOMO_MOCK_MODE=false`` and does not pass ``--simulate``, so the mock cannot swallow a confirmed start.
    The interpreter is this one, so the dashboard's virtualenv is the one that runs the hand-off.  The shell
    text stays the documented command.
    """
    data_dir = Path(data_dir)
    argv = [sys.executable, str(ROOT / "main.py"), "--from-plan", str(int(plan_id))]
    env = {"ASTRONOMO_DATA_DIR": str(data_dir)}
    if rehearse:
        argv.extend(["--simulate", "--fast"])
    else:
        env["ASTRONOMO_MOCK_MODE"] = "false"
    prefix = _data_prefix(data_dir, default_data_dir)
    if not rehearse:
        prefix.append("ASTRONOMO_MOCK_MODE=false")
    command = " ".join([*prefix, "python", "main.py", *argv[2:]])
    return LaunchSpec(plan_id=int(plan_id), rehearse=rehearse, argv=argv, env=env, command=command)


def stop_command(data_dir: Path, default_data_dir: Path, mission_id: str | None = None) -> str:
    """The command that parks the telescope and leaves the mission stopped."""
    body = "python main.py --stop" + (f" {mission_id}" if mission_id else "")
    return " ".join([*_data_prefix(data_dir, default_data_dir), body])


def pid_alive(pid: int | None) -> bool:
    """Whether ``pid`` is still doing work.

    A finished child stays in the process table until its parent collects it, and ``kill(pid, 0)`` still
    succeeds for that leftover.  Collect it here.  A permission error means the process exists and is not ours.
    """
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    try:
        if os.waitpid(pid, os.WNOHANG)[0]:
            return False  # it had already exited; the entry is gone now
    except ChildProcessError:
        pass  # not a child of this process
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _launches_path(data_dir: Path) -> Path:
    return Path(data_dir) / "plan_runs" / "launches.json"


def read_launches(data_dir: Path) -> dict[str, dict]:
    path = _launches_path(data_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_launches(data_dir: Path, launches: dict[str, dict]) -> None:
    path = _launches_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(launches, indent=2), encoding="utf-8")
    tmp.replace(path)


def launch_plan(spec: LaunchSpec, *, data_dir: Path, popen: Callable[..., Any] = subprocess.Popen) -> dict:
    """Start the hand-off in its own session and remember its pid.  A second start is refused while it lives.

    The child's own session means closing the dashboard does not kill the mission.  stdout goes to
    ``plan_runs/<id>-<time>.log`` so a refusal ("cannot start") is still on the card after the process exits.
    """
    data_dir = Path(data_dir)
    current = read_launches(data_dir).get(str(spec.plan_id))
    if current and pid_alive(current.get("pid")):
        raise LaunchError(f"Plan #{spec.plan_id} already has a process running (pid {current['pid']}).")
    runs = data_dir / "plan_runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = runs / f"{spec.plan_id}-{stamp}.log"
    env = os.environ.copy()
    env.update(spec.env)
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        proc = popen(
            spec.argv, cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except Exception:
        log_file.close()
        raise
    log_file.close()
    record = {
        "plan_id": spec.plan_id,
        "pid": proc.pid,
        "rehearse": spec.rehearse,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "log": str(log_path),
        "command": spec.command,
    }
    launches = read_launches(data_dir)
    launches[str(spec.plan_id)] = record
    _write_launches(data_dir, launches)
    return record


def countdown_label(start: datetime | None, end: datetime | None, now: datetime) -> tuple[str, str]:
    """``(2 h 05 m, until start)`` before the window, ``Now`` while it is open, ``Closed`` after it ends."""
    if start is None:
        return "-", ""
    if now < start:
        return fmt_span((start - now).total_seconds()), "until start"
    if end is not None and now >= end:
        return "Closed", "window ended"
    return "Now", "window open"


def mission_id_from_log(path: str | None) -> str | None:
    """The mission a start printed (``=== Mission m_... ===``). A rehearsal is not stored on the plan, so the log is the link."""
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    try:
        with file.open(encoding="utf-8", errors="replace") as handle:
            for i, line in enumerate(handle):
                if i > 400:
                    break
                match = re.search(r"=== Mission (\S+)", line)
                if match:
                    return match.group(1)
    except OSError:
        return None
    return None


def log_tail(path: str | None, lines: int = _LOG_TAIL) -> str:
    if not path:
        return ""
    file = Path(path)
    if not file.is_file():
        return ""
    try:
        text = file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(text[-lines:])


def _mission_running(db, plan: dict, lease_s: float) -> bool:
    mission_id = plan.get("mission_id")
    if not mission_id:
        return False
    mission = db.get_mission(mission_id)
    return bool(mission) and liveness(mission, lease_s) == "running"

