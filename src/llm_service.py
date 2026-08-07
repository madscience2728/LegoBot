"""
llm_service.py -- manual entry point for the LLM backend.

Standalone for now, started by hand:

    python3 llm_service.py

Eventually main.py should grow a flag (e.g. --llm, alongside its
existing --deploy) that runs this alongside the GUI instead of you
needing to start it separately -- not wired up yet, on purpose, same
reasoning main.py's own docstring gives for --deploy being separate
from the GUI: prove the piece works standalone before folding it into
the coordinated entry point.

What this does:
    1. Constructs a DockerGemma4Adapter and runs its health check
       (adapter.load()). All the actual "is the server up, what do I do
       if not" logic already lives in the adapter itself -- if the
       Docker llama.cpp server isn't reachable at config.json's
       lm_config.base_url, adapter.load() prints the exact `docker run`
       command and raises SystemExit.
    2. If healthy, runs ONE real test tick through run_tick() with a
       hardcoded sample state, using your actual model -- not a fake
       adapter like the sandbox test that proved run_tick's wiring.
       Prints the raw model output, the validated Action (or the
       rejection reason), and token counts.

This is still a one-shot script, not an ongoing loop -- it proves the
full chain works on your real hardware/model once, then exits. The
ongoing tick loop (repeated ticks, feeding real sensor state instead
of this hardcoded sample, streaming to the UI) is still not built.
"""
import asyncio
import sys
from pathlib import Path

# Makes this runnable directly as `python3 src/llm_service.py` (which
# puts src/ itself on sys.path, not the project root) as well as
# `python3 -m src.llm_service` from the root (which already gets this
# right on its own) -- without this, the absolute `from src.adapter...`
# import below only works in the second case.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapter.docker_gemma4_adapter import DockerGemma4Adapter
from src.adapter.tick_runner import run_tick

# Hardcoded stand-in for real gathered state (memory system, position
# tracking, etc. don't exist yet). Good enough to prove the model
# actually receives the prompt correctly and responds in the right
# shape -- swap this out once real state-gathering exists.
_SAMPLE_STATE = {
    "name": "LegoBot",
    "current_expression": "neutral",
    "current_head_tilt": "forward",
    "memories_returned": {
        "10 seconds ago": "I remember being powered on",
    },
}


async def _check_and_tick():
    print("== LLM service: checking Docker Gemma 4 backend ==")
    adapter = DockerGemma4Adapter(enable_thinking=False)
    await adapter.load()
    print("[LLM service reachable and healthy]")

    print()
    print("== Running one real test tick ==")
    result = await run_tick(adapter, _SAMPLE_STATE)

    print(f"raw model output: {result.tick.raw!r}")
    print(f"prompt_tokens={result.prompt_tokens} completion_tokens={result.completion_tokens}")

    if result.tick.ok:
        print(f"[TICK OK] used_autofix={result.tick.used_autofix}")
        print(f"action: {result.tick.action}")
    else:
        print(f"[TICK REJECTED] errors: {result.tick.errors}")


def main():
    try:
        asyncio.run(_check_and_tick())
    except SystemExit as e:
        # adapter.load() already printed the docker run command and
        # raised SystemExit itself -- just propagate a clean process
        # exit here rather than adding a second, redundant message.
        sys.exit(e.code if isinstance(e.code, int) else 1)


if __name__ == "__main__":
    main()
