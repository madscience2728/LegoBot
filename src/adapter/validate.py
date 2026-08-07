"""
validate.py -- turns a parsed-but-untrusted dict into either a clean
Action, or a list of reasons it's unusable.

Design stance (per project decision): "cosmetic" issues -- an
out-of-range number, an extra unexpected key -- get silently clamped
or ignored, not rejected. Only structural problems (missing required
field, wrong type, value that isn't even in the right universe, e.g.
an unknown direction string) count as validation failures that bubble
up to the caller. The caller decides what a failure means for the
tick; this module only decides what's valid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema import (
    DRIVE_DIRECTIONS,
    EXPRESSIONS,
    HEAD_ORIENTATIONS,
    REQUIRED_DRIVE_KEYS,
    REQUIRED_TOP_LEVEL_KEYS,
    SPEED_RANGE,
    duration_range_for,
)


@dataclass(frozen=True)
class Drive:
    direction: str
    speed: float
    duration: float


@dataclass(frozen=True)
class Action:
    speech_out: str
    next_expression: str
    next_head_orientation: str
    drive: Drive


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):  # bool is an int subclass -- reject explicitly
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def validate_and_clamp(data: Any) -> tuple[Action | None, list[str]]:
    """Returns (action, errors). action is None iff errors is non-empty."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return None, [f"top-level output must be an object, got {type(data).__name__}"]

    missing = REQUIRED_TOP_LEVEL_KEYS - data.keys()
    if missing:
        errors.append(f"missing top-level keys: {sorted(missing)}")
        return None, errors

    speech_out = data["speech_out"]
    if not isinstance(speech_out, str):
        errors.append(f"speech_out must be a string, got {type(speech_out).__name__}")

    expression = data["next_expression"]
    if not isinstance(expression, str) or expression.lower() not in EXPRESSIONS:
        errors.append(f"next_expression {expression!r} not in {sorted(EXPRESSIONS)}")

    head = data["next_head_orientation"]
    if not isinstance(head, str) or head.upper() not in HEAD_ORIENTATIONS:
        errors.append(f"next_head_orientation {head!r} not in {sorted(HEAD_ORIENTATIONS)}")

    drive_raw = data["drive"]
    if not isinstance(drive_raw, dict):
        errors.append(f"drive must be an object, got {type(drive_raw).__name__}")
        return None, errors

    drive_missing = REQUIRED_DRIVE_KEYS - drive_raw.keys()
    if drive_missing:
        errors.append(f"drive missing keys: {sorted(drive_missing)}")
        return None, errors

    direction = drive_raw["direction"]
    if not isinstance(direction, str) or direction.upper() not in DRIVE_DIRECTIONS:
        errors.append(f"drive.direction {direction!r} not in {sorted(DRIVE_DIRECTIONS)}")

    if errors:
        return None, errors

    direction = direction.upper()
    expression = expression.lower()
    head = head.upper()

    speed = _coerce_float(drive_raw["speed"])
    if speed is None:
        return None, [f"drive.speed must be numeric, got {drive_raw['speed']!r}"]
    speed = _clamp(speed, *SPEED_RANGE)

    duration = _coerce_float(drive_raw["duration"])
    if duration is None:
        return None, [f"drive.duration must be numeric, got {drive_raw['duration']!r}"]

    dur_range = duration_range_for(direction)
    if dur_range is None:
        # STATIONARY: duration is meaningless: normalize to 0 rather
        # than trusting whatever number the model attached to it.
        duration = 0.0
        speed = 0.0
    else:
        duration = _clamp(duration, *dur_range)

    action = Action(
        speech_out=str(speech_out),
        next_expression=expression,
        next_head_orientation=head,
        drive=Drive(direction=direction, speed=speed, duration=duration),
    )
    return action, []
