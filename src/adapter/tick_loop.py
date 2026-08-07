"""
tick_loop.py -- repeating version of the one-shot run_tick, so the
robot keeps sensing/responding continuously instead of firing once and
exiting.

This exists specifically because of audio timing: gui/server.py's
audio_ring only holds about 6 seconds of buffered PCM (see
AUDIO_BUFFER_FRAMES there). A one-shot script that starts, ticks once,
and exits has no guaranteed relationship to when someone actually
speaks -- you'd have to get lucky with timing. A repeating loop means
there's always a tick coming soon that will pick up whatever's
currently in the buffer.

Deliberately still takes a `build_state` callback rather than owning
any state itself -- state-gathering (currently the hardcoded sample in
llm_service.py) and memory (not built yet, per the project roadmap:
senses -> feedback -> driving/memory/face id, in that order) are both
still someone else's job. This loop only owns the timing.
"""
from __future__ import annotations

import asyncio
from typing import Callable

import httpx

from .docker_gemma4_adapter import DockerGemma4Adapter
from .senses import fetch_latest_senses
from .tick_runner import TickRunResult, run_tick


async def run_forever(
    adapter: DockerGemma4Adapter,
    gui_base_url: str,
    build_state: Callable[[], dict],
    interval_s: float = 3.0,
    on_tick: Callable[[TickRunResult], None] | None = None,
):
    """Runs run_tick on a fixed interval, forever, pulling fresh
    sight/hearing from gui/server.py's /api/senses/latest before each
    call.

    Args:
        adapter: already-.load()-ed DockerGemma4Adapter.
        gui_base_url: where gui/server.py's FastAPI app is running,
            e.g. "http://127.0.0.1:8000".
        build_state: called fresh each tick to get the CURRENT STATE
            INPUT dict. A function, not a static dict, specifically so
            a future real memory system can plug in without this loop
            changing at all.
        interval_s: seconds between tick STARTS (not between end of one
            and start of the next) -- if a tick takes longer than this
            to complete, the next one starts immediately rather than
            stacking up a backlog. 3s is a reasonable starting point:
            long enough that model latency doesn't cause pileup, short
            enough that the ~6-second audio ring doesn't roll over an
            entire utterance before it's ever sent.
        on_tick: optional callback invoked with each TickRunResult
            (e.g. print/log it, or eventually dispatch the resulting
            Action to the phone). Runs synchronously between ticks --
            keep it fast, or hand off to your own asyncio.create_task
            from inside it if it needs to do real work.
    """
    async with httpx.AsyncClient(timeout=10.0) as senses_client:
        while True:
            loop = asyncio.get_event_loop()
            tick_start = loop.time()

            senses = await fetch_latest_senses(gui_base_url, client=senses_client)
            state = build_state()
            result = await run_tick(adapter, state, senses=senses)

            if on_tick is not None:
                on_tick(result)

            elapsed = loop.time() - tick_start
            await asyncio.sleep(max(0.0, interval_s - elapsed))
