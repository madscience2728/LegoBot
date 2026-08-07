"""
autofix.py -- best-effort repair of near-JSON text into parseable JSON.

This is intentionally a *chain of cheap, reversible text transforms*,
not a general parser. Each stage tries json.loads() after its fix; the
first stage that parses wins. If nothing works, auto_fix_json returns
None and the caller (loop.py) skips the tick entirely, per the design
decision: no fabricated fallback command, just no-op until the next
valid tick.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
# Matches a JSON-looking object: first '{' to the LAST '}' in the text.
# Greedy on purpose -- LLMs sometimes wrap the object in prose before
# and after it, and we want the whole object, not the first nested one.
_OBJECT_SPAN_RE = re.compile(r"\{.*\}", re.DOTALL)


def _try_parse(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _strip_code_fence(text: str) -> str:
    m = _CODE_FENCE_RE.search(text)
    return m.group(1) if m else text


def _extract_object_span(text: str) -> str:
    m = _OBJECT_SPAN_RE.search(text)
    return m.group(0) if m else text


def _strip_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _normalize_smart_quotes(text: str) -> str:
    return (
        text.replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2018", "'").replace("\u2019", "'")
    )


def _single_to_double_quotes(text: str) -> str:
    # Cheap heuristic, not a real tokenizer: only applied as a late-stage
    # fallback, after every safer fix has already failed to parse.
    return text.replace("'", '"')


def auto_fix_json(raw: str) -> Optional[dict]:
    """Attempts a sequence of increasingly aggressive repairs. Returns
    the parsed dict on first success, or None if every stage fails.

    Two-phase, not just a flat candidate list:

    Phase A ("gentle" -- fence stripping, smart-quote normalization):
    if any gentle candidate is ALREADY valid JSON of some kind, we
    trust that the model produced syntactically intentional output.
    If it parsed to a dict, return it. If it parsed to anything else
    (a list, a bare string/number), that's a SCHEMA problem, not a
    syntax problem -- auto_fix_json must not go dig a dict out of a
    structure the model deliberately gave a different top-level shape.
    That's validate.py's job to reject, not this module's job to paper
    over by guessing which nested object the model "really meant".

    Phase B (aggressive object rescue -- object-span extraction,
    trailing-comma removal, single-quote conversion): only attempted
    if NONE of the gentle candidates parsed as valid JSON at all, i.e.
    the text is genuinely malformed and needs real repair.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None

    gentle_candidates = [raw]
    stripped = _strip_code_fence(raw)
    gentle_candidates.append(stripped)
    normalized = _normalize_smart_quotes(stripped)
    gentle_candidates.append(normalized)

    any_gentle_candidate_parsed = False
    for candidate in gentle_candidates:
        result = _try_parse(candidate)
        if result is not None or candidate.strip() in ("null", "None"):
            any_gentle_candidate_parsed = True
            if isinstance(result, dict):
                return result

    if any_gentle_candidate_parsed:
        # Valid JSON, but not a dict (e.g. a top-level array/string) --
        # a real structural mismatch, not something autofix should
        # "rescue" by digging into it.
        return None

    # Nothing parsed cleanly -- text is genuinely malformed. Now it's
    # safe to try more aggressive, riskier rewrites.
    span = _extract_object_span(normalized)
    no_trailing_commas = _strip_trailing_commas(span)
    single_quoted_fix = _single_to_double_quotes(no_trailing_commas)

    for candidate in (span, no_trailing_commas, single_quoted_fix):
        result = _try_parse(candidate)
        if isinstance(result, dict):
            return result

    return None
