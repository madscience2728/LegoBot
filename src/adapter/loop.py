"""
loop.py -- orchestrates a single tick's worth of adapter logic.

Deliberately has ZERO network/hardware code in it -- no requests to
the phone, no LLM API call. process_tick() is a pure function: raw
text in, TickResult out. That's what makes it testable with synthetic
strings instead of a live model or robot, per the "prove the JSON
contract first" plan. The real loop (not built yet) will be a thin
wrapper that calls the LLM, feeds the text here, and dispatches
result.action to gui/server.py's /api/* endpoints only when
result.ok is True.

Failure policy (per project decision): auto-fix first; if that also
fails to produce a valid Action, skip the tick entirely -- no
fabricated fallback command. The caller is expected to simply not
dispatch anything and let the robot hold its last commanded state.
"""
from __future__ import annotations

from dataclasses import dataclass

from .autofix import auto_fix_json
from .validate import Action, validate_and_clamp


@dataclass(frozen=True)
class TickResult:
    ok: bool
    action: Action | None
    used_autofix: bool
    errors: list[str]
    raw: str


def process_tick(raw_llm_output: str) -> TickResult:
    """Parses + validates one tick of raw LLM text output.

    Order of operations:
      1. Try strict json.loads first (no repair) -- most ticks should
         hit this path if the model is behaving.
      2. If that fails, run auto_fix_json's repair chain.
      3. Whichever parse succeeded gets validated/clamped against the
         schema.
      4. Any failure at any stage -> ok=False, action=None. The tick
         is skipped, full stop -- not a partial/best-guess action.
    """
    import json

    used_autofix = False
    parsed = None

    try:
        parsed = json.loads(raw_llm_output)
        if not isinstance(parsed, dict):
            parsed = None
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if parsed is None:
        parsed = auto_fix_json(raw_llm_output)
        used_autofix = parsed is not None

    if parsed is None:
        return TickResult(
            ok=False, action=None, used_autofix=used_autofix,
            errors=["could not parse JSON, even after auto-fix"],
            raw=raw_llm_output,
        )

    action, errors = validate_and_clamp(parsed)
    if action is None:
        return TickResult(
            ok=False, action=None, used_autofix=used_autofix,
            errors=errors, raw=raw_llm_output,
        )

    return TickResult(
        ok=True, action=action, used_autofix=used_autofix,
        errors=[], raw=raw_llm_output,
    )
