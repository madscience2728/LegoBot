"""
tick_runner.py -- the piece that was still missing: calls the model
with real state, takes its raw text back, and feeds it through
loop.process_tick() (file 4). Everything upstream of this (schema,
autofix, validate, loop, the Docker adapter, token_guard) was already
built and independently tested; this is the wire connecting them.

Deliberately thin. It does not gather robot state itself (no memory
system, no position tracking exists yet) and does not dispatch the
resulting Action to the phone's /api/* endpoints (that's gui/server.py's
job). It takes state in, returns a TickResult out -- same "pure
function, hardware is someone else's problem" shape as process_tick
itself, just one layer up.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .docker_gemma4_adapter import DockerGemma4Adapter
from .loop import TickResult, process_tick
from .prompt import build_system_prompt


@dataclass(frozen=True)
class TickRunResult:
    tick: TickResult
    prompt_tokens: int
    completion_tokens: int


async def run_tick(
    adapter: DockerGemma4Adapter,
    state: dict,
    persona_preamble: str = "",
) -> TickRunResult:
    """Runs one full tick: state dict in, validated (or rejected)
    Action out, via a real call to the model.

    Args:
        adapter: an already-constructed, already-.load()-ed
            DockerGemma4Adapter (see llm_service.py for the health
            check that proves it's reachable before you get here).
        state: the CURRENT STATE INPUT dict -- name, expression,
            head tilt, memories, sensory placeholders, etc. Built by
            the caller each tick; this function doesn't know or care
            where it came from.
        persona_preamble: optional personality/identity text prepended
            to the system prompt, ahead of the OUTPUT FORMAT spec.
            Left blank by default since that's the brain-sim's concern,
            not this file's.

    Returns:
        TickRunResult wrapping the TickResult from process_tick, plus
        token counts from the model call for logging/cost tracking.
        If the model call itself fails (timeout, HTTP error), the
        adapter's own complete_async already catches that and returns
        an empty NodeResult -- which process_tick will then correctly
        reject as unparseable, same skip-the-tick behavior as any other
        bad output. No special-casing needed here for that case.
    """
    system_prompt = build_system_prompt(persona_preamble)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(state)},
    ]

    node_result = await adapter.complete_async(messages)
    tick_result = process_tick(node_result.content)

    return TickRunResult(
        tick=tick_result,
        prompt_tokens=node_result.prompt_tokens,
        completion_tokens=node_result.completion_tokens,
    )
