"""The data firewall between deterministic code and any language model.

Rule of Astronomo: **everything heavy is reduced in plain Python** (astropy, photutils,
scipy) and a model only ever sees a short, rounded JSON *digest*.  Raw FITS arrays, pixel
cut-outs, per-frame source tables, WCS dumps and catalog rows never leave the process.

This module makes that rule structural instead of a convention.  Every prompt is built by
:func:`render_payload` (or checked by :func:`assert_text_safe`), which **rejects** -- it does
not silently truncate -- payloads that look like bulk data:

* non-JSON objects (numpy arrays, astropy tables/HDUs, bytes, paths ...)
* lists that hold more than a handful of numbers (what ``ndarray.tolist()`` produces)
* too many numbers overall, or nesting that is too deep
* strings that are FITS-header dumps, runs of numbers, or base64-style blobs
* anything whose rendered size exceeds the character budget

Rejecting is deliberate: a truncated data summary would silently mislead the model, and a
large payload means a plugin forgot to reduce something -- that should be loud.

Plugin authors use :func:`compact` / :func:`scalars_only` / :func:`round_floats` to build
digests that pass.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np


class LLMPayloadError(ValueError):
    """Raised when data destined for a language model violates the data-reduction rules."""


@dataclass(frozen=True)
class PayloadLimits:
    max_chars: int = 6000  # rendered JSON size
    max_numeric_list: int = 12  # numbers allowed in any single list (nested lists counted flat)
    max_total_numbers: int = 250  # numeric leaves in the whole payload
    max_depth: int = 5
    max_str: int = 600


DEFAULT_LIMITS = PayloadLimits()

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER_RUN = re.compile(rf"(?:{_NUMBER}[\s,;\[\]()]+){{24,}}")
_FITS_CARD = re.compile(r"^(SIMPLE|BITPIX|NAXIS\d?|XTENSION)\s*=", re.MULTILINE)
_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{200,}")
_FILE_LIKE = re.compile(r"\.(fits?|fts|png|jpe?g|pdf|npy|npz)$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Detection of bulk data
# --------------------------------------------------------------------------- #
def looks_like_data_dump(text: str) -> str | None:
    """Return a reason if ``text`` looks like bulk data rather than prose/short JSON."""
    if _FITS_CARD.search(text):
        return "looks like a FITS header dump"
    if _NUMBER_RUN.search(text):
        return "contains a long run of bare numbers (array/table dump)"
    if _BLOB.search(text):
        return "contains a long opaque token (binary/base64 blob)"
    return None


def _count_numbers(obj: Any, cap: int) -> int:
    """Count numeric leaves in nested lists, stopping early once ``cap`` is exceeded."""
    total = 0
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (list, tuple)):
            stack.extend(cur)
        elif isinstance(cur, bool):
            continue
        elif isinstance(cur, (int, float, np.number)):
            total += 1
        if total > cap:
            break
    return total


def find_violations(obj: Any, limits: PayloadLimits = DEFAULT_LIMITS) -> list[str]:
    """List every way ``obj`` breaks the data-reduction rules (empty list => safe)."""
    problems: list[str] = []
    numbers = 0

    def walk(o: Any, path: str, depth: int) -> None:
        nonlocal numbers
        if len(problems) >= 8:
            return
        if depth > limits.max_depth:
            problems.append(f"{path}: nested deeper than {limits.max_depth} levels")
            return
        if o is None or isinstance(o, bool):
            return
        if isinstance(o, np.generic):
            o = o.item()
        if isinstance(o, (int, float)):
            numbers += 1
            if isinstance(o, float) and not math.isfinite(o):
                problems.append(f"{path}: non-finite number")
            return
        if isinstance(o, str):
            if len(o) > limits.max_str:
                problems.append(f"{path}: string of {len(o)} chars exceeds {limits.max_str}")
            elif (why := looks_like_data_dump(o)) is not None:
                problems.append(f"{path}: string {why}")
            return
        if isinstance(o, Mapping):
            for k, v in o.items():
                if not isinstance(k, str):
                    problems.append(f"{path}: non-string key {k!r}")
                    return
                walk(v, f"{path}.{k}" if path else k, depth + 1)
            return
        if isinstance(o, (list, tuple)):
            n = _count_numbers(o, limits.max_numeric_list)
            if n > limits.max_numeric_list:
                problems.append(f"{path or '<root>'}: list holds more than {limits.max_numeric_list} numbers (array-like data)")
                return  # do not walk a potentially huge array
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]", depth + 1)
            return
        hint = " (reduce it to a few statistics first)" if hasattr(o, "shape") or hasattr(o, "__array__") else ""
        problems.append(f"{path or '<root>'}: {type(o).__name__} is not plain JSON data{hint}")

    walk(obj, "", 0)
    if not problems and numbers > limits.max_total_numbers:
        problems.append(f"payload holds {numbers} numbers (limit {limits.max_total_numbers}); summarise it further")
    return problems


def _to_plain(obj: Any) -> Any:
    """Convert numpy *scalars* to Python scalars; leave arrays alone so they get flagged."""
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, Mapping):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


def render_payload(obj: Any, limits: PayloadLimits = DEFAULT_LIMITS) -> str:
    """Render ``obj`` as compact JSON, or raise :class:`LLMPayloadError`."""
    plain = _to_plain(obj)
    problems = find_violations(plain, limits)
    if problems:
        raise LLMPayloadError("refusing to send data to the language model: " + "; ".join(problems))
    text = json.dumps(plain, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(text) > limits.max_chars:
        raise LLMPayloadError(f"data payload is {len(text)} chars (limit {limits.max_chars}); summarise it further")
    return text


def assert_text_safe(text: str, max_chars: int) -> None:
    """Last-line check on a fully assembled prompt (system + task + data)."""
    if len(text) > max_chars:
        raise LLMPayloadError(f"prompt is {len(text)} chars (limit {max_chars}); summarise the data further")
    why = looks_like_data_dump(text)
    if why is not None:
        raise LLMPayloadError(f"refusing to send prompt: it {why}")


# --------------------------------------------------------------------------- #
# Helpers for building digests
# --------------------------------------------------------------------------- #
def round_sig(x: float, sig: int = 5) -> float:
    if not isinstance(x, (int, float)) or isinstance(x, bool) or x == 0 or not math.isfinite(x):
        return x
    return round(x, sig - 1 - int(math.floor(math.log10(abs(x)))))


def round_floats(obj: Any, sig: int = 5) -> Any:
    """Recursively round floats to ``sig`` significant figures (fewer digits, fewer tokens)."""
    obj = _to_plain(obj)
    if isinstance(obj, float):
        return round_sig(obj, sig) if math.isfinite(obj) else None
    if isinstance(obj, Mapping):
        return {k: round_floats(v, sig) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, sig) for v in obj]
    return obj


def short(text: Any, n: int = 300) -> str | None:
    if text is None:
        return None
    s = str(text).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def scalars_only(mapping: Mapping, *, exclude: tuple[str, ...] = (), max_str: int = 200) -> dict:
    """Keep only simple scalar values (drops containers, arrays and file-like strings)."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if key in exclude:
            continue
        value = _to_plain(value)
        if value is None or isinstance(value, (bool, int)):
            out[key] = value
        elif isinstance(value, float):
            out[key] = round_sig(value) if math.isfinite(value) else None
        elif isinstance(value, str) and len(value) <= max_str and not _FILE_LIKE.search(value):
            out[key] = value
    return out


def _array_stats(values: Any) -> dict:
    arr = np.asarray(values, dtype=float).ravel()
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "min": round_sig(float(arr.min())),
        "median": round_sig(float(np.median(arr))),
        "max": round_sig(float(arr.max())),
    }


def compact(obj: Any, *, max_items: int = 8, max_str: int = 300, sig: int = 5, depth: int = 4) -> Any:
    """Shrink an arbitrary structure into something that passes the firewall.

    * numeric lists / numpy arrays  -> ``{"n", "min", "median", "max"}``
    * long lists                    -> ``{"first": [...], "n_total": N}``
    * long strings                  -> truncated;  unknown objects -> ``"<TypeName>"``
    """
    obj = _to_plain(obj)
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    if isinstance(obj, float):
        return round_sig(obj, sig) if math.isfinite(obj) else None
    if isinstance(obj, str):
        if looks_like_data_dump(obj):  # truncating a dump would still leave a dump
            return f"<omitted: {len(obj)}-char data string>"
        return short(obj, max_str)
    if depth <= 0:
        return "<omitted: too deep>"
    if isinstance(obj, np.ndarray):
        return _array_stats(obj)
    if isinstance(obj, Mapping):
        return {str(k): compact(v, max_items=max_items, max_str=max_str, sig=sig, depth=depth - 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        if obj and _count_numbers(obj, max_items) > max_items and all(
            isinstance(v, (int, float, list, tuple)) and not isinstance(v, bool) for v in obj
        ):
            try:
                return _array_stats(obj)
            except (TypeError, ValueError):
                pass
        items = [compact(v, max_items=max_items, max_str=max_str, sig=sig, depth=depth - 1) for v in obj[:max_items]]
        return items if len(obj) <= max_items else {"first": items, "n_total": len(obj)}
    return f"<{type(obj).__name__}>"


def default_summary(findings: Mapping, crossref: Mapping | None = None, target: Mapping | None = None) -> dict:
    """Default-deny digest used when a plugin does not override ``summarize_for_llm``.

    Only scalar top-level values and the *size* of every container survive: a plugin that wants
    the model to see per-detection detail must say so explicitly by overriding the hook.
    """
    out: dict[str, Any] = {}
    if target:
        out["target"] = scalars_only(target, exclude=("db_id",))
    out["summary"] = short(findings.get("summary"))
    scalars = scalars_only(findings, exclude=("summary",))
    for key, value in findings.items():
        if isinstance(value, (list, tuple, dict)):
            scalars[f"n_{key}"] = len(value)
    out["findings"] = scalars
    if crossref:
        out["crossref_summary"] = short(crossref.get("summary"))
        out["n_crossref_entries"] = len(crossref.get("entries") or [])
    return out


# --------------------------------------------------------------------------- #
# Grounding check for generated prose
# --------------------------------------------------------------------------- #
_PROSE_NUMBER = re.compile(rf"(?<![\w.]){_NUMBER}(?![\w])")


def _decimals(token: str) -> int:
    mantissa = re.split(r"[eE]", token)[0]
    return len(mantissa.split(".")[1]) if "." in mantissa else 0


def ungrounded_numbers(text: str, payload_json: str) -> list[str]:
    """Numbers in ``text`` that the data payload does not support.

    A number is *grounded* if it equals a payload number up to rounding at its own precision
    (``0.3`` is grounded by ``0.252``) or within 1 %.  This is a tripwire for invented figures,
    not a proof of correctness: it cannot tell whether a grounded number is used correctly.
    """
    source = [float(m.group()) for m in _PROSE_NUMBER.finditer(payload_json)]
    bad: list[str] = []
    for m in _PROSE_NUMBER.finditer(text):
        token = m.group()
        try:
            value = float(token)
        except ValueError:
            continue
        tol_round = 0.5 * 10 ** (-_decimals(token)) + 1e-9
        if not any(abs(value - s) <= max(tol_round, 0.01 * abs(s)) for s in source) and token not in bad:
            bad.append(token)
    return bad
