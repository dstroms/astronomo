from plugins.asteroid_finder.plugin import AsteroidFinderPlugin

#: Convention: every plugin package exposes its class as ``PLUGIN_CLASS``.
PLUGIN_CLASS = AsteroidFinderPlugin

__all__ = ["AsteroidFinderPlugin", "PLUGIN_CLASS"]
