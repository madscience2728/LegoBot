"""
dispatch.py -- sends a validated Action's expression, speech, and now
drive command to the phone, via gui/server.py's EXISTING /api/face/set,
/api/voice/say, and /api/drive proxies (which themselves forward to
ProbeService.kt on the phone). No new phone-side code needed -- all
three endpoints were already built and working from the manual
dashboard controls; /api/drive's own docstring literally says it's
"the actual command surface the future LLM layer will call."

Deliberately goes through gui/server.py rather than hitting the phone
directly, same reasoning as senses.py: gui/server.py is the only thing
that should know the phone's IP or hold connection state. Every
LLM-side call, inbound (senses) or outbound (face/voice/drive), goes
through it, not just one direction.

speech_out == "" is the model's explicit "stay silent" choice, and
drive.direction == "STATIONARY" is the explicit "don't move" choice --
same convention, and deliberately independent of each other: the robot
can stay silent while driving, or speak while stationary. Skipping one
must never skip the other (an earlier version of this file had that
exact bug -- the silent-speech path returned early, before the drive
dispatch even ran).

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
Pacing WHEN new speech/driving gets generated (so the model doesn't
even try something new until the last thing finished) belongs in
tick_loop.py, via wait_until_speech_done below -- that's a timing
concern for the caller, not a "should I allow this" gate here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from .validate import Action

# Maps schema.py's DRIVE_DIRECTIONS to gui/server.py's DRIVE_COMMANDS
# vocabulary -- these are deliberately different names on each side
# (FORWARD vs go_forward, etc.) since schema.py's enum was designed
# around the original robot-facing spec, while DRIVE_COMMANDS matches
# ProbeService.kt's actual /drive endpoint. STATIONARY has no entry
# here on purpose -- it's not a command to send, it's "send nothing."
_DRIVE_COMMAND_MAP = {
    "FORWARD": "go_forward",
    "BACKWARDS": "go_back",
    "TURN_LEFT": "turn_left",
    "TURN_RIGHT": "turn_right",
}


@dataclass(frozen=True)
class DispatchResult:
    face_ok: bool
    face_error: str | None
    voice_ok: bool
    voice_error: str | None
    voice_skipped_silent: bool = False
    drive_ok: bool = True
    drive_error: str | None = None
    drive_skipped_stationary: bool = False

    @property
    def ok(self) -> bool:
        return self.face_ok and self.voice_ok and self.drive_ok


async def dispatch_action(
    action: Action,
    gui_base_url: str,
    client: httpx.AsyncClient,
) -> DispatchResult:
    """POSTs the action's expression, speech, and drive command to the
    phone via gui/server.py. Failures are caught and reported, never
    raised -- a dispatch failure (phone briefly unreachable, one
    dropped request) shouldn't take down the tick loop; the next tick
    tries again with fresh state regardless, same "skip and move on"
    philosophy as process_tick's own failure handling.

    Face, voice, and drive are three INDEPENDENT dispatches -- skipping
    one (silent speech, stationary drive) never skips another.
    """
    face_ok, face_error = await _post(
        client, f"{gui_base_url}/api/face/set", {"expression": action.next_expression}
    )

    voice_ok, voice_error, voice_skipped_silent = True, None, False
    if not action.speech_out:
        # Explicit "stay silent" -- validate.py already stripped
        # whitespace-only strings down to "", so this catches both the
        # model literally emitting "" and it emitting "   ".
        voice_skipped_silent = True
    else:
        voice_ok, voice_error = await _post(
            client, f"{gui_base_url}/api/voice/say", {"text": action.speech_out, "interrupt": True}
        )

    drive_ok, drive_error, drive_skipped_stationary = True, None, False
    if action.drive.direction == "STATIONARY":
        drive_skipped_stationary = True
    else:
        command = _DRIVE_COMMAND_MAP[action.drive.direction]
        drive_ok, drive_error = await _post(
            client, f"{gui_base_url}/api/drive",
            {"command": command, "speed": action.drive.speed, "duration_s": action.drive.duration},
        )

    return DispatchResult(
        face_ok=face_ok, face_error=face_error,
        voice_ok=voice_ok, voice_error=voice_error, voice_skipped_silent=voice_skipped_silent,
        drive_ok=drive_ok, drive_error=drive_error, drive_skipped_stationary=drive_skipped_stationary,
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
        resp = await client.post(url, json=json_body, timeout=15.0)
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
