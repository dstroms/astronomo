"""Plugin registry / dynamic loader.

A plugin is a sub-package of ``plugins/`` whose ``__init__.py`` exposes
``PLUGIN_CLASS`` (a :class:`core.base_plugin.BaseExperimentPlugin` subclass).
Nothing in the core imports a concrete plugin; ``main.py`` calls :func:`load_plugin`
with the name from ``config.ACTIVE_PLUGIN``.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from config import Settings
from core.base_plugin import PLUGIN_STATUSES, BaseExperimentPlugin


class PluginLoadError(RuntimeError):
    pass


def available_plugins() -> list[str]:
    root = Path(__file__).parent
    return sorted(m.name for m in pkgutil.iter_modules([str(root)]) if m.ispkg)


def plugin_status(name: str) -> tuple[str, str]:
    """``(status, note)`` of plugin ``name`` without building it.  Never raises: a plugin that will not import is a draft."""
    try:
        cls = getattr(importlib.import_module(f"plugins.{name}"), "PLUGIN_CLASS", None)
        status = str(getattr(cls, "status", "ready"))
        return (status if status in PLUGIN_STATUSES else "draft"), str(getattr(cls, "status_note", "") or "")
    except Exception:  # noqa: BLE001 - listing plugins must work even when one is broken
        return "draft", "it failed to import"


def draft_refusal(plugin: BaseExperimentPlugin, settings: Settings) -> str | None:
    """Why ``plugin`` may not drive this run, or ``None``.

    A draft plugin is fine in simulation and on an archive replay (no hardware moves).  It is refused on a real
    telescope unless the person set ``ASTRONOMO_ALLOW_DRAFT_PLUGINS``.
    """
    if plugin.status != "draft":
        return None
    if settings.mock_mode or settings.archive_mode or settings.allow_draft_plugins:
        return None
    why = f" ({plugin.status_note})" if plugin.status_note else ""
    return (
        f"plugin '{plugin.name}' is a draft and not ready for use{why}, so it will not command a real telescope. "
        "Run it with --simulate or --data-source archive, use a ready plugin, or set ASTRONOMO_ALLOW_DRAFT_PLUGINS=1 "
        "if you accept that."
    )


def load_plugin(name: str, settings: Settings) -> BaseExperimentPlugin:
    """Import ``plugins.<name>`` and instantiate its ``PLUGIN_CLASS``."""
    if name not in available_plugins():
        raise PluginLoadError(f"Unknown plugin '{name}'. Available: {', '.join(available_plugins()) or 'none'}")
    try:
        module = importlib.import_module(f"plugins.{name}")
    except Exception as exc:  # surface import errors with context
        raise PluginLoadError(f"Plugin '{name}' failed to import: {exc}") from exc

    cls = getattr(module, "PLUGIN_CLASS", None)
    if cls is None or not (isinstance(cls, type) and issubclass(cls, BaseExperimentPlugin)):
        raise PluginLoadError(f"plugins.{name} must expose PLUGIN_CLASS subclassing BaseExperimentPlugin")
    plugin = cls(settings)
    if plugin.name != name:
        raise PluginLoadError(f"Plugin package '{name}' reports name '{plugin.name}'; they must match")
    if plugin.status not in PLUGIN_STATUSES:
        raise PluginLoadError(f"Plugin '{name}' has status {plugin.status!r}; it must be one of {', '.join(PLUGIN_STATUSES)}")
    source = getattr(settings, "data_source", "telescope")
    if source not in plugin.data_sources:
        raise PluginLoadError(
            f"Plugin '{name}' cannot run on data source '{source}' (supports: {', '.join(plugin.data_sources)})"
        )
    return plugin
