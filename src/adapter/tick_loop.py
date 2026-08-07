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
still someone else's job. This loop only owns the timing, dispatching
a valid tick's result back to the phone, and (see below) yielding
until that result has actually finished playing out before generating
the next one.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

import httpx

from .dispatch import DispatchResult, dispatch_action, wait_until_speech_done
from .docker_gemma4_adapter import DockerGemma4Adapter
from .senses import fetch_latest_senses
from .tick_runner import TickRunResult, run_tick


async def run_forever(
    adapter: DockerGemma4Adapter,
    gui_base_url: str,
    build_state: Callable[[], dict],
    interval_s: float = 3.0,
    on_tick: Callable[[TickRunResult, Optional[DispatchResult]], None] | None = None,
    dispatch: bool = True,
):
    """Runs run_tick on a fixed interval, forever, pulling fresh
    sight/hearing from gui/server.py's /api/senses/latest before each
    call, and (by default) dispatching a successful tick's expression
    + speech back to the phone.

    Args:
        adapter: already-.load()-ed DockerGemma4Adapter.
        gui_base_url: where gui/server.py's FastAPI app is running,
            e.g. "http://127.0.0.1:8000".
        build_state: called fresh each tick to get the CURRENT STATE
            INPUT dict. A function, not a static dict, specifically so
            a future real memory system can plug in without this loop
            changing at all.
        interval_s: MINIMUM seconds between tick STARTS when the
            previous tick had nothing to wait for (silent + stationary,
            or a rejected tick). When the previous tick spoke or would
            have driven, the loop waits for that to actually finish
            first (see below) -- interval_s never adds extra delay on
            top of that, it's a floor, not a fixed cadence.
        on_tick: optional callback invoked with (TickRunResult,
            DispatchResult | None) after each tick. DispatchResult is
            None when the tick was rejected (nothing to dispatch) or
            when dispatch=False. Runs synchronously between ticks --
            keep it fast.
        dispatch: when True (default) and a tick is valid, POSTs
            next_expression + speech_out to the phone via dispatch.py.
            Set False to run senses-only, e.g. for debugging without
            the phone visibly reacting.

    Why this yields on speech/drive duration before looping again:
    the previous design fired a new tick on a fixed timer regardless of
    whether the phone was still speaking, which could cut a sentence
    off mid-utterance every few seconds. Waiting here instead means a
    new tick, and therefore a new model call, doesn't happen until
    there's actually room for its result. Note this is a PACING
    mechanism only -- dispatch.py's dispatch_action() itself has no
    "still speaking, skip" gate (an earlier version did, and it caused
    a real bug: see dispatch.py's module docstring for why that check
    was removed). The phone's own interrupt=true/QUEUE_FLUSH already
    handles any overlap correctly if this pacing is ever bypassed.
    """
    async with httpx.AsyncClient(timeout=10.0) as senses_client:
        while True:
            loop = asyncio.get_event_loop()
            tick_start = loop.time()

            senses = await fetch_latest_senses(gui_base_url, client=senses_client)
            state = build_state()
            result = await run_tick(adapter, state, senses=senses)

            dispatch_result = None
            if dispatch and result.tick.ok:
                dispatch_result = await dispatch_action(result.tick.action, gui_base_url, senses_client)

                # Yield until the utterance we JUST sent has actually
                # finished, so the next loop iteration's fresh model
                # call can't generate a reply with nowhere to go.
                spoke_something_new = dispatch_result.voice_ok and not dispatch_result.voice_skipped_silent
                if spoke_something_new:
                    await wait_until_speech_done(senses_client, gui_base_url)

                # Driving isn't dispatched to the phone yet -- still out
                # of scope per the roadmap -- but pacing ticks around
                # the duration the MODEL ITSELF chose is free
                # correctness to build in now. When driving is wired up
                # for real, this loop won't need revisiting: it already
                # can't generate a fresh instruction before the last one
                # would have finished playing out.
                drive = result.tick.action.drive
                if drive.direction != "STATIONARY" and drive.duration > 0:
                    await asyncio.sleep(drive.duration)

            if on_tick is not None:
                on_tick(result, dispatch_result)

            elapsed = loop.time() - tick_start
            await asyncio.sleep(max(0.0, interval_s - elapsed))
