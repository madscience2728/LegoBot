"""
prompt.py -- builds the system prompt's OUTPUT FORMAT spec directly
from schema.py's enums and ranges, instead of hand-writing a second
copy of them in prompt text. If FaceBus.kt's expression list changes,
you update schema.py (file 1) and this prompt updates itself -- there
is exactly one place that knows what's valid, matching the same
"single source of truth" reasoning schema.py's own docstring gives.
"""
from __future__ import annotations

import json

from .schema import (
    DRIVE_DIRECTIONS,
    EXPRESSIONS,
    HEAD_ORIENTATIONS,
    LONG_DURATION_RANGE,
    SHORT_DURATION_RANGE,
    SPEED_RANGE,
    _LONG_DURATION_DIRECTIONS,
    _SHORT_DURATION_DIRECTIONS,
)

_OUTPUT_SHAPE_EXAMPLE = {
    "speech_out": "what you want to say here",
    "next_expression": f"[one of: {', '.join(sorted(EXPRESSIONS))}]",
    "next_head_orientation": f"[one of: {', '.join(sorted(HEAD_ORIENTATIONS))}]",
    "drive": {
        "direction": f"[one of: {', '.join(sorted(DRIVE_DIRECTIONS))}]",
        "speed": f"[{SPEED_RANGE[0]}-{SPEED_RANGE[1]}, 1.0 recommended in most cases]",
        "duration": (
            f"[{LONG_DURATION_RANGE[0]}-{LONG_DURATION_RANGE[1]} seconds when "
            f"direction is {'/'.join(sorted(_LONG_DURATION_DIRECTIONS))}] | "
            f"[{SHORT_DURATION_RANGE[0]}-{SHORT_DURATION_RANGE[1]} seconds when "
            f"direction is {'/'.join(sorted(_SHORT_DURATION_DIRECTIONS))}] | "
            f"[0 when STATIONARY]"
        ),
    },
}


def build_system_prompt(persona_preamble: str = "") -> str:
    """Returns the full system prompt: an optional persona/personality
    preamble (left blank here -- that's the brain-sim's job, not this
    file's) followed by the OUTPUT FORMAT spec generated from schema.py.

    Deliberately does NOT include the CURRENT STATE INPUT shape here --
    that's per-tick data (memories, senses, expression) built fresh
    each call by whatever constructs the user message, not a static
    part of the system prompt.
    """
    format_block = json.dumps(_OUTPUT_SHAPE_EXAMPLE, indent=4)

    sections = []
    if persona_preamble:
        sections.append(persona_preamble.strip())

    sections.append(
        "Every message, you will receive a JSON object describing your "
        "current state, recent memories, and sensory input. You must "
        "respond with ONLY a valid JSON object in exactly this shape "
        "(the bracketed text in each value describes what's allowed "
        "there -- your actual response must use a real value, not the "
        "bracketed description itself):\n\n"
        f"{format_block}\n\n"
        "Respond with the JSON object and nothing else -- no prose "
        "before or after it, no markdown code fences."
    )

    return "\n\n".join(sections)
