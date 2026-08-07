"""
dispatch.py -- sends a validated Action's expression and speech to the
phone, via gui/server.py's EXISTING /api/face/set and /api/voice/say
proxies (which themselves forward to ProbeService.kt on the phone).
No new phone-side code needed -- these endpoints were already built
and working from the manual dashboard controls.

Deliberately goes through gui/server.py rather than hitting the phone
directly, same reasoning as senses.py: gui/server.py is the only thing
that should know the phone's IP or hold connection state. Every
LLM-side call, inbound (senses) or outbound (face/voice), goes through
it, not just one direction.

Driving is NOT dispatched here -- explicitly still out of scope per
the project roadmap (senses -> feedback -> driving/memory/face id, in
that order). This module only ever sends next_expression and
speech_out; action.drive is read and validated by the adapter layer
but never acted on yet.

speech_out == "" is the model's explicit "stay silent" choice, same
convention as drive.direction=STATIONARY -- skips the /voice/say call
entirely, no empty utterance sent.

IMPORTANT, learned the hard way: this module does NOT pre-check
whether the phone is still speaking before sending new speech.
VoiceController.kt's speak(text, interrupt=true) already uses
TextToSpeech.QUEUE_FLUSH, which cleanly stops-and-replaces whatever's
currently playing on the phone side -- there's nothing for a
Python-side pre-check to protect against that the phone doesn't
already handle correctly. An earlier version of this file DID add such
a check (querying /api/voice/status and skipping if "speaking" was
true), and it caused total, permanent loss of voice output: Android's
TTS engine has a known quirk where a FLUSHED utterance doesn't always
fire its completion callback, which can leave "speaking" stuck true in
VoiceController's tracking set forever. That pre-check trusted that
flag completely and silenced all future speech once it got stuck.
Pacing WHEN new speech gets generated (so the model doesn't even try to
say something new until the last thing finished) belongs in
tick_loop.py, via wait_until_speech_done below -- that's a timing
concern for the caller, not a "should I allow this" gate here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from .validate import Action


@dataclass(frozen=True)
class DispatchResult:
    face_ok: bool
    face_error: str | None
    voice_ok: bool
    voice_error: str | None
    voice_skipped_silent: bool = False

    @property
    def ok(self) -> bool:
        return self.face_ok and self.voice_ok


async def dispatch_action(
    action: Action,
    gui_base_url: str,
    client: httpx.AsyncClient,
) -> DispatchResult:
    """POSTs the action's expression and speech to the phone via
    gui/server.py. Failures are caught and reported, never raised --
    a dispatch failure (phone briefly unreachable, one dropped request)
    shouldn't take down the tick loop; the next tick tries again with
    fresh state regardless, same "skip and move on" philosophy as
    process_tick's own failure handling.

    Speech is always attempted (unless explicitly silent) -- no
    still-speaking pre-check. See module docstring for why.
    """
    face_ok, face_error = await _post(
        client, f"{gui_base_url}/api/face/set", {"expression": action.next_expression}
    )

    if not action.speech_out:
        # Explicit "stay silent" -- validate.py already stripped
        # whitespace-only strings down to "", so this catches both the
        # model literally emitting "" and it emitting "   ".
        return DispatchResult(
            face_ok=face_ok, face_error=face_error,
            voice_ok=True, voice_error=None, voice_skipped_silent=True,
        )

    voice_ok, voice_error = await _post(
        client, f"{gui_base_url}/api/voice/say", {"text": action.speech_out, "interrupt": True}
    )
    return DispatchResult(
        face_ok=face_ok, face_error=face_error,
        voice_ok=voice_ok, voice_error=voice_error,
    )


async def _is_still_speaking(client: httpx.AsyncClient, gui_base_url: str) -> bool:
    """Checks gui/server.py's /api/voice/status. Fails open (returns
    False) on any error. Used ONLY by wait_until_speech_done below, as
    a pacing signal -- never as a gate on whether to allow speech (see
    module docstring for why that distinction matters).
    """
    try:
        resp = await client.get(f"{gui_base_url}/api/voice/status", timeout=8.0)
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return False
        return bool(data.get("speaking", False))
    except Exception:
        return False


async def wait_until_speech_done(
    client: httpx.AsyncClient,
    gui_base_url: str,
    poll_interval_s: float = 0.3,
    max_wait_s: float = 20.0,
) -> None:
    """Blocks until the phone reports it's done speaking, or
    max_wait_s elapses. Purely a PACING helper for tick_loop.py: the
    caller awaits this after dispatching speech, so the NEXT tick's
    fresh model output isn't generated until there's actually room for
    it to be said. This is not a gate on whether speech is ALLOWED
    (see dispatch_action/module docstring) -- it only affects timing.

    If max_wait_s is reached without ever seeing speaking=False, that's
    treated as a stuck "speaking" flag (the known TTS-flush quirk
    described in the module docstring) rather than a genuinely
    20-second-long sentence -- POST /api/voice/stop to force-clear it
    (VoiceController.stop() explicitly clears its tracking set) so the
    NEXT check reports correctly instead of staying stuck forever.
    """
    waited = 0.0
    while waited < max_wait_s:
        if not await _is_still_speaking(client, gui_base_url):
            return
        await asyncio.sleep(poll_interval_s)
        waited += poll_interval_s

    try:
        await client.post(f"{gui_base_url}/api/voice/stop", json={}, timeout=8.0)
    except Exception:
        pass  # best-effort self-heal; a failure here just means we try again next time


async def _post(client: httpx.AsyncClient, url: str, json_body: dict) -> tuple[bool, str | None]:
    try:
        resp = await client.post(url, json=json_body, timeout=10.0)
        data = resp.json()
        # gui/server.py's proxies return {"status": "error", ...} with a
        # 4xx/5xx status for known failure cases (e.g. "not connected to
        # a phone") -- check the body, not just the HTTP status, since
        # that's the actual signal these endpoints use.
        if isinstance(data, dict) and data.get("status") == "error":
            return False, data.get("message", "unknown error")
        return True, None
    except Exception as e:
        return False, str(e)
