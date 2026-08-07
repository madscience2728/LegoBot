"""
llm_service.py -- the LLM backend supervisor.

Run standalone by hand:

    python3 src/llm_service.py

Or, normally, just:

    python3 main.py

-- main.py now always starts this alongside the GUI, in one process,
no flag needed. See main.py's docstring for why one process instead of
two separate ones.

What this does:
    run_llm_service() is a supervisor loop that NEVER crashes the
    process on a Docker/model failure. If the Docker llama.cpp server
    isn't reachable, it reports that (via the on_status callback, and
    to the console either way) and waits -- either for
    AUTO_RETRY_INTERVAL_S to pass, or for retry_event to be set
    (main.py wires this to the GUI's "Try again" button via
    POST /api/llm/retry) -- then tries again. Once connected, it runs
    the repeating tick loop (tick_loop.run_forever) same as before.

    This used to raise SystemExit straight out of adapter.load() on
    failure, which took the whole process down -- including the GUI,
    even though the GUI has nothing to do with whether Docker happens
    to be running. That's been moved to a plain catchable exception
    (DockerServerUnavailable, in docker_gemma4_adapter.py) specifically
    so this loop can catch it and keep going instead.
"""
import asyncio
import sys
from pathlib import Path
from typing import Callable, Optional

# Makes this runnable directly as `python3 src/llm_service.py` (which
# puts src/ itself on sys.path, not the project root) as well as
# `python3 -m src.llm_service` from the root (which already gets this
# right on its own) -- without this, the absolute `from src.adapter...`
# imports below only work in the second case.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapter.dispatch import DispatchResult
from src.adapter.docker_gemma4_adapter import DockerGemma4Adapter, DockerServerUnavailable
from src.adapter.tick_loop import run_forever
from src.adapter.tick_runner import TickRunResult

# Where gui/server.py's FastAPI app is running -- same machine, so
# localhost. Change this if you ever run the GUI server elsewhere.
GUI_BASE_URL = "http://127.0.0.1:8000"

TICK_INTERVAL_S = 3.0

# How long to wait between automatic retry attempts if nobody clicks
# "try again" -- so leaving it running with Docker off and then
# starting Docker later still recovers on its own within this window.
AUTO_RETRY_INTERVAL_S = 10.0

# Hardcoded stand-in for real gathered state (memory system, position
# tracking, etc. don't exist yet -- deliberately out of scope until
# senses + feedback are both proven). Good enough to prove the model
# actually receives sight/hearing correctly each tick.
_SAMPLE_STATE = {
    "name": "LegoBot",
    "current_expression": "neutral",
    "current_head_tilt": "forward",
    "memories_returned": {
        "10 seconds ago": "I remember being powered on",
    },
}


def _build_state() -> dict:
    # Function, not a constant, so a real memory/state system can
    # replace just this one function later without touching the loop.
    return dict(_SAMPLE_STATE)


def _print_tick(result: TickRunResult, dispatch_result: Optional[DispatchResult]) -> None:
    print()
    senses_str = f"vision={'yes' if result.had_vision else 'NO'} audio={'yes' if result.had_audio else 'NO'}"
    print(f"senses this tick: {senses_str}")
    print(f"prompt_tokens={result.prompt_tokens} completion_tokens={result.completion_tokens}")
    if result.tick.ok:
        print(f"[TICK OK] used_autofix={result.tick.used_autofix}")
        print(f"action: {result.tick.action}")
        if dispatch_result is not None:
            if not dispatch_result.face_ok:
                print(f"[DISPATCH FAILED] face: {dispatch_result.face_error}")

            if dispatch_result.voice_skipped_silent:
                print("[VOICE] skipped -- model chose to stay silent")
            elif not dispatch_result.voice_ok:
                print(f"[DISPATCH FAILED] voice: {dispatch_result.voice_error}")
            else:
                print("[VOICE] sent to phone")

            if dispatch_result.drive_skipped_stationary:
                print("[DRIVE] skipped -- stationary")
            elif not dispatch_result.drive_ok:
                print(f"[DISPATCH FAILED] drive: {dispatch_result.drive_error}")
            else:
                print("[DRIVE] sent to phone")
    else:
        print(f"[TICK REJECTED] errors: {result.tick.errors}")
        print(f"raw: {result.tick.raw!r}")


async def _wait_before_retry(retry_event: Optional[asyncio.Event]) -> None:
    """Waits for either AUTO_RETRY_INTERVAL_S to pass, or retry_event
    to be set (the GUI's "Try again" button), whichever comes first.
    Always clears the event afterward so a stale set() doesn't cause
    an immediate second retry next time through.
    """
    if retry_event is None:
        await asyncio.sleep(AUTO_RETRY_INTERVAL_S)
        return
    try:
        await asyncio.wait_for(retry_event.wait(), timeout=AUTO_RETRY_INTERVAL_S)
    except asyncio.TimeoutError:
        pass
    finally:
        retry_event.clear()


async def run_llm_service(
    on_status: Optional[Callable[[dict], None]] = None,
    retry_event: Optional[asyncio.Event] = None,
) -> None:
    """The supervisor loop. Runs forever (until cancelled) -- alternates
    between "try to connect + run ticks" and "report the failure and
    wait for a retry", never raising out of here on a Docker failure.

    Args:
        on_status: called with a dict every time connection status
            changes -- {"status": "connecting"}, {"status": "connected"},
            or {"status": "error", "message": ..., "docker_command": ...}.
            main.py wires this to update gui/server.py's State and
            broadcast it to the browser. None (standalone CLI use)
            just skips this -- console printing below still happens
            either way.
        retry_event: an asyncio.Event the caller can .set() to trigger
            an immediate retry instead of waiting out
            AUTO_RETRY_INTERVAL_S. main.py wires this to the GUI's
            "Try again" button. None (standalone CLI use) means only
            automatic retries happen -- there's no button to wire in a
            plain terminal.
    """
    def _report(status: dict):
        if on_status is not None:
            on_status(status)

    while True:
        print("== LLM service: checking Docker Gemma 4 backend ==")
        _report({"status": "connecting"})

        adapter = DockerGemma4Adapter(enable_thinking=False)
        try:
            await adapter.load()
        except DockerServerUnavailable as e:
            _report({
                "status": "error",
                "message": str(e),
                "docker_command": e.docker_command,
            })
            print(f"[LLM service] not connected -- retrying in up to {AUTO_RETRY_INTERVAL_S:.0f}s "
                  f"(or immediately if retried from the GUI)")
            await _wait_before_retry(retry_event)
            continue

        print("[LLM service reachable and healthy]")
        _report({"status": "connected"})

        print()
        print(f"== Starting repeating tick loop (every {TICK_INTERVAL_S}s) ==")
        print(f"Pulling senses from {GUI_BASE_URL}")

        # run_forever() only returns if cancelled (Ctrl+C / process
        # shutdown) -- there's no "Docker died mid-loop" recovery here
        # yet, since complete_async already catches httpx errors itself
        # and returns an empty result rather than raising. A future
        # improvement could detect a long run of empty results and loop
        # back to a fresh health check; out of scope for this change.
        await run_forever(
            adapter,
            GUI_BASE_URL,
            _build_state,
            interval_s=TICK_INTERVAL_S,
            on_tick=_print_tick,
        )
        return  # pragma: no cover -- only reached if run_forever ever returns normally


def main():
    """Standalone entry point: `python3 src/llm_service.py`. Same
    supervisor loop as the GUI-embedded version, just with no status
    callback (prints to console instead) and no retry button (only
    the automatic retry timer applies)."""
    try:
        asyncio.run(run_llm_service())
    except KeyboardInterrupt:
        print("\n[LLM service stopped]")


if __name__ == "__main__":
    main()
