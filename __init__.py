"""Obol: know when Hermes will run out of budget, and act before it does. See _plugin.py."""
try:
    from ._plugin import Obol, _safe, register  # noqa: F401
except ImportError:
    if __package__:          # a real failure inside Hermes; outside a package (pytest's rootdir walk) it's moot
        raise
