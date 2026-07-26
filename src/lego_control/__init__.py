"""lego_control -- the shared BLE/pylgbst logic. Owns zero networking;
imported directly by both the Pi relay and the PC bypass path so there's
exactly one implementation of every hub command."""

__version__ = "0.1.0"
