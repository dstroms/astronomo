from plugins.transit_finder.plugin import TransitFinderPlugin

#: Convention: every plugin package exposes its class as ``PLUGIN_CLASS``.
PLUGIN_CLASS = TransitFinderPlugin

__all__ = ["TransitFinderPlugin", "PLUGIN_CLASS"]
