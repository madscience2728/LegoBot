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

import base64
import json
from dataclasses import dataclass

from .docker_gemma4_adapter import DockerGemma4Adapter
from .loop import TickResult, process_tick
from .prompt import build_system_prompt
from .senses import SensesSnapshot


@dataclass(frozen=True)
class TickRunResult:
    tick: TickResult
    prompt_tokens: int
    completion_tokens: int
    had_vision: bool
    had_audio: bool


def _build_user_content(state: dict, senses: SensesSnapshot | None) -> str | list[dict]:
    """Plain string (text-only, the original shape) if there's no
    vision/audio to attach; a multimodal content-block list otherwise.

    The image block ("image_url" with a data: URI) is the well-
    established llama.cpp/OpenAI vision format and should work with
    the mmproj already configured. The audio block ("input_audio") is
    genuinely UNVERIFIED for this setup -- gemma-4's mmproj is a vision
    projector, not an audio one, and there's no evidence this specific
    model/server combination understands raw audio input at all. It's
    included so we can actually find out by trying it, not because
    it's confirmed to work -- check llm_service.py's output for
    whether the server errors on this field or silently ignores it.
    """
    if senses is None or not (senses.has_vision or senses.has_audio):
        return json.dumps(state)

    content: list[dict] = [{"type": "text", "text": json.dumps(state)}]

    if senses.has_vision:
        content.append({
            "type": "image_url",
            "image_url": {"url": senses.image_data_uri()},
        })

    if senses.has_audio:
        wav_b64 = base64.b64encode(senses.wav_bytes).decode("ascii")
        content.append({
            "type": "input_audio",
            "input_audio": {"data": wav_b64, "format": "wav"},
        })

    return content


async def run_tick(
    adapter: DockerGemma4Adapter,
    state: dict,
    persona_preamble: str = "",
    senses: SensesSnapshot | None = None,
) -> TickRunResult:
    """Runs one full tick: state dict (+ optionally sight/hearing) in,
    validated (or rejected) Action out, via a real call to the model.

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
        senses: optional SensesSnapshot from senses.fetch_latest_senses.
            When provided and it actually has vision and/or audio, the
            user message becomes a multimodal content list instead of
            a plain JSON string -- see _build_user_content's docstring
            for the vision-confirmed / audio-unverified distinction.

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
        {"role": "user", "content": _build_user_content(state, senses)},
    ]

    node_result = await adapter.complete_async(messages)
    tick_result = process_tick(node_result.content)

    return TickRunResult(
        tick=tick_result,
        prompt_tokens=node_result.prompt_tokens,
        completion_tokens=node_result.completion_tokens,
        had_vision=senses.has_vision if senses is not None else False,
        had_audio=senses.has_audio if senses is not None else False,
    )
