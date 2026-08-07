"""
schema.py -- the single source of truth for what a valid LLM tick
output looks like. Mirrors the phone's REAL accepted vocabulary
(FaceBus.EXPRESSIONS, DriveController's DRIVE_COMMANDS, WheelMap)
rather than the free-emoji sketch in the original design doc --
a closed enum is a far smaller/safer surface than "emit arbitrary
Unicode and hope it renders", same reasoning FaceBus.kt's own doc
comment gives for having EXPRESSIONS at all.

Nothing in this file talks to the network or the phone. Pure data.
"""
from __future__ import annotations

# Mirrors FaceBus.kt's EXPRESSIONS map exactly (names only -- emoji
# rendering is the phone's job, not the adapter's).
EXPRESSIONS = frozenset({
    "neutral", "happy", "excited", "curious", "confused",
    "sad", "sleepy", "surprised", "listening", "speaking",
    "error", "off",
})

# Mirrors ProbeService.kt's DRIVE_COMMANDS tilt_head_* trio, collapsed
# to the three orientation states the design doc's "next_head_orientation"
# actually needs (UP/FORWARD/DOWN -> tilt_head_up/tilt_head_center/tilt_head_down
# is a mapping concern for the dispatch layer, not this schema).
HEAD_ORIENTATIONS = frozenset({"UP", "FORWARD", "DOWN"})

# Mirrors the design doc's drive.direction enum. STATIONARY is the
# fail-safe/no-op state -- it deliberately has no duration semantics.
DRIVE_DIRECTIONS = frozenset({
    "STATIONARY", "FORWARD", "BACKWARDS", "TURN_RIGHT", "TURN_LEFT",
})

# Directions that mean "actually move" and therefore need a bounded
# duration. STATIONARY is excluded on purpose.
_MOVING_DIRECTIONS = DRIVE_DIRECTIONS - {"STATIONARY"}

# Per the design doc: forward gets a longer leash (0.0-5.0s), turning
# or reversing is capped much tighter (0.0-0.33s) since those are the
# actions most likely to send the robot somewhere unrecoverable if the
# model picks a big number.
_LONG_DURATION_DIRECTIONS = frozenset({"FORWARD"})
_SHORT_DURATION_DIRECTIONS = frozenset({"BACKWARDS", "TURN_RIGHT", "TURN_LEFT"})

SPEED_RANGE = (0.0, 1.0)
LONG_DURATION_RANGE = (0.0, 5.0)
SHORT_DURATION_RANGE = (0.0, 0.33)

REQUIRED_TOP_LEVEL_KEYS = frozenset({
    "speech_out", "next_expression", "next_head_orientation", "drive",
})
REQUIRED_DRIVE_KEYS = frozenset({"direction", "speed", "duration"})


def duration_range_for(direction: str) -> tuple[float, float] | None:
    """Returns the (min, max) allowed duration for a direction, or None
    if the direction doesn't use duration at all (STATIONARY)."""
    if direction in _LONG_DURATION_DIRECTIONS:
        return LONG_DURATION_RANGE
    if direction in _SHORT_DURATION_DIRECTIONS:
        return SHORT_DURATION_RANGE
    return None
