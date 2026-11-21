#!/usr/bin/env python3
"""From a fresh clone to a working simulation.

    python3 install.py

Checks that Python is 3.10 or newer, creates ``.venv``, installs the project, and
writes ``.env``. It asks only for the upload identity (observer name, AAVSO code,
MPC station code, observing site). Each answer can be left blank.

A second run leaves an existing ``.env`` alone unless you agree to replace it.
Without a terminal:

    python3 install.py --non-interactive
    python3 install.py --non-interactive --replace-env --aavso-code AB1 --site-lat 39.7 --site-lon -105 --site-elev 1600

The telescope mock stays on. Turning it off is the real-hardware page, not this command.
Nothing here is sent anywhere, and no API key is asked for or written.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_PYTHON = (3, 10)


class SetupError(Exception):
    """The answers cannot be written. The message says what to change."""


def python_too_old(version: tuple[int, ...]) -> str | None:
    if version < MIN_PYTHON:
        found = ".".join(str(n) for n in version[:3])
        return f"error: Astronomo needs Python 3.10 or newer. This interpreter is {found}."
    return None


def venv_python(root: Path) -> Path:
    return root / ".venv" / "bin" / "python"


def running_in_venv(root: Path) -> bool:
    return Path(sys.prefix).resolve() == (root / ".venv").resolve()


def ensure_venv(root: Path, run) -> Path:
    """Create ``.venv`` and install into it. A later run installs again if the command is missing."""
    py = venv_python(root)
    if not py.exists():
        run([sys.executable, "-m", "venv", str(root / ".venv")], cwd=root)
    if not (py.parent / "astronomo").exists():
        run([str(py), "-m", "pip", "install", "-e", ".[dev]"], cwd=root)
    return py


def _quote(value: str) -> str:
    if any(ch in value for ch in " #\""):
        return '"' + value.replace('"', "") + '"'
    return value


def render_env(example: str, *, observer_name: str, aavso_code: str, mpc_code: str, site: dict | None) -> str:
    """The example file, plus the identity lines that were answered. The mock stays on."""
    lines = [example.rstrip("\n"), "", "# Written by install.py. The telescope mock stays on."]
    if observer_name:
        lines.append(f"ASTRONOMO_OBSERVER_NAME={_quote(observer_name)}")
    if aavso_code:
        lines.append(f"ASTRONOMO_AAVSO_OBS_CODE={aavso_code}")
    if mpc_code:
        lines.append(f"ASTRONOMO_MPC_STATION_CODE={mpc_code}")
    if site is not None:
        lines.append(f"ASTRONOMO_SITE_LAT={site['lat']}")
        lines.append(f"ASTRONOMO_SITE_LON={site['lon']}")
        lines.append(f"ASTRONOMO_SITE_ELEV_M={site['elev']}")
        if site.get("name"):
            lines.append(f"ASTRONOMO_SITE_NAME={_quote(site['name'])}")
    return "\n".join(lines) + "\n"


def _answers_from(args: argparse.Namespace, interactive: bool) -> tuple[str, str, str, dict | None]:
    observer = _ask("Observer name (MPC submitter, optional)", args.observer_name, interactive)
    aavso = _ask("AAVSO observer code (1-5 letters or digits, or leave empty)", args.aavso_code, interactive)
    mpc = _ask("MPC station code (three letters or digits, or leave empty)", args.mpc_code, interactive)
    site = _ask_site(args, interactive)
    return observer.strip(), aavso.strip(), mpc.strip(), site


def _ask(label: str, preset: str | None, interactive: bool) -> str:
    if preset is not None:
        return preset
    if not interactive:
        return ""
    return input(f"{label}: ").strip()


def _ask_site(args: argparse.Namespace, interactive: bool) -> dict | None:
    given = (args.site_lat, args.site_lon, args.site_elev)
    if any(v is not None for v in given) and not all(v is not None for v in given):
        raise SetupError("error: a site needs latitude, longitude, and elevation together (east-positive longitude).")
    if all(v is not None for v in given):
        return {"lat": args.site_lat, "lon": args.site_lon, "elev": args.site_elev, "name": args.site_name or ""}
    if not interactive:
        return None
    print("Observing site. Leave the latitude blank to skip it. Longitude is east-positive.")
    lat = input("Latitude (blank to skip): ").strip()
    if not lat:
        return None
    lon = input("Longitude: ").strip()
    elev = input("Elevation in metres: ").strip()
    name = input("Site name (optional): ").strip()
    if not lon or not elev:
        raise SetupError("error: a site needs latitude, longitude, and elevation together (east-positive longitude).")
    return {"lat": lat, "lon": lon, "elev": elev, "name": name}


def _check(observer_name: str, aavso_code: str, mpc_code: str, site: dict | None) -> dict | None:
    import config
    from core.site import SiteError, validated_site

    try:
        config.check_upload_identity(aavso_code, mpc_code)
    except ValueError as exc:
        raise SetupError(f"error: {exc}") from exc
    if site is None:
        return None
    try:
        checked = validated_site(site.get("name") or "", site["lat"], site["lon"], site["elev"])
    except SiteError as exc:
        raise SetupError(f"error: {exc}") from exc
    return {"lat": checked["lat"], "lon": checked["lon"], "elev": checked["elev_m"], "name": site.get("name") or ""}


def apply_setup(root: Path, args: argparse.Namespace, *, interactive: bool, run=subprocess.run) -> int:
    """Write ``.env`` and, unless skipped, run a fast simulation. Returns a process status."""
    root = root.resolve()
    example = root / ".env.example"
    dest = root / ".env"
    if not example.is_file():
        print(f"error: {example} is missing.", file=sys.stderr)
        return 1
    if dest.exists() and not args.replace_env:
        if not interactive:
            print("error: .env already exists. Re-run with --replace-env to overwrite it.", file=sys.stderr)
            return 1
        answer = input(".env already exists. Replace it? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Left the existing .env in place.")
            return _finish(root, args, run, did_write=False)
    try:
        observer, aavso, mpc, site = _answers_from(args, interactive)
        site = _check(observer, aavso, mpc, site)
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    text = render_env(example.read_text(encoding="utf-8"), observer_name=observer, aavso_code=aavso, mpc_code=mpc, site=site)
    dest.write_text(text, encoding="utf-8")
    print(f"Wrote {dest}.")
    return _finish(root, args, run, did_write=True)


def _finish(root: Path, args: argparse.Namespace, run, *, did_write: bool) -> int:
    status = 0
    if not args.no_self_test:
        print("Running a fast simulation.")
        env = os.environ.copy()
        env.pop("ASTRONOMO_NO_DOTENV", None)
        env["ASTRONOMO_DOTENV"] = str(root / ".env")
        if args.data_dir is not None:
            env["ASTRONOMO_DATA_DIR"] = str(Path(args.data_dir))
        completed = run([sys.executable, str(ROOT / "main.py"), "--simulate", "--fast"], cwd=ROOT, env=env)
        status = completed.returncode if completed.returncode is not None else 1
        if status != 0:
            print("error: the simulation did not finish. .env was still written." if did_write else "error: the simulation did not finish.", file=sys.stderr)
            return status
    print("Next: python -m dashboard.api")
    print("Then open http://localhost:8765")
    print("Before a real telescope, read docs/real-hardware.md. This setup left the telescope mock on.")
    return status


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    too_old = python_too_old(sys.version_info)
    if too_old:
        print(too_old, file=sys.stderr)
        return 1
    root = (args.root or ROOT).resolve()
    interactive = not args.non_interactive and sys.stdin.isatty()
    if not args.no_install and not running_in_venv(root):
        try:
            py = ensure_venv(root, _check_call)
        except subprocess.CalledProcessError as exc:
            print(f"error: the environment could not be installed ({exc}).", file=sys.stderr)
            return exc.returncode or 1
        os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])
    return apply_setup(root, args, interactive=interactive)


def _check_call(cmd, cwd=None):
    subprocess.check_call(cmd, cwd=cwd)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Install Astronomo and write .env. The telescope mock stays on.")
    p.add_argument("--non-interactive", action="store_true", help="Ask nothing. Unset answers stay unset.")
    p.add_argument("--replace-env", action="store_true", help="Overwrite an existing .env. Without this, an existing file is kept.")
    p.add_argument("--no-self-test", action="store_true", help="Skip the fast simulation at the end.")
    p.add_argument("--no-install", action="store_true", help="Skip creating .venv. For a machine that is already installed, and for tests.")
    p.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    p.add_argument("--data-dir", type=Path, default=None, help=argparse.SUPPRESS)
    p.add_argument("--observer-name", default=None)
    p.add_argument("--aavso-code", default=None)
    p.add_argument("--mpc-code", default=None)
    p.add_argument("--site-name", default=None)
    p.add_argument("--site-lat", default=None)
    p.add_argument("--site-lon", default=None)
    p.add_argument("--site-elev", default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
