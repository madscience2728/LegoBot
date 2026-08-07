"""
main.py -- single entry point for LegoBot, run from the PC.

Usage:
    python3 main.py           # start the GUI dashboard AND the LLM
                               # supervisor loop together, at
                               # http://localhost:8000. Blocks until
                               # Ctrl+C. If Docker/the model isn't
                               # reachable, the dashboard shows an alert
                               # with a "Try again" button instead of
                               # anything crashing -- see
                               # src/llm_service.py's run_llm_service().

    python3 main.py --deploy  # build + push the Android probe app to the
                               # phone (android_project/deploy.py), then
                               # exit. Doesn't touch the GUI at all -- a
                               # one-off maintenance action, same
                               # separation the old Pi-era main.py (see
                               # old/main.py) drew between running the
                               # thing and pushing code to it.

Why one process instead of two: gui/server.py owns the only WebSocket
connection to the phone's media relay (media_relay_loop) and is what
src/llm_service.py's tick loop polls for senses over HTTP
(/api/senses/latest). gui.server.serve_async() and
src.llm_service.run_llm_service() are both plain coroutines scheduled
together via asyncio.gather() -- the tick loop still talks to the GUI
over the same HTTP endpoint either way, this just avoids needing two
terminals open.

Why the LLM side can't take the GUI down anymore: run_llm_service() is
a supervisor loop, not a one-shot check -- a missing Docker container
reports as a retryable status (surfaced in the dashboard) instead of
raising. See src/adapter/docker_gemma4_adapter.py's
DockerServerUnavailable for the exception that replaced the old
SystemExit-on-failure behavior.

What this does NOT do (yet):
    Route commands anywhere beyond what the GUI's Command Console sends
    to the phone's dummy /command endpoint. The old Pi-era routing logic
    (old/src/lego_brain/robot_client.py, pc_server.py) is archived under
    old/ and isn't wired up here -- there's no robot in the loop at this
    stage, on purpose.
"""
import argparse
import asyncio


def _run_deploy():
    print("== Deploying android_project to the phone ==")
    from android_project.deploy import main as deploy_main
    deploy_main()


def _run_gui_and_llm():
    from gui.server import serve_async, state
    from src.llm_service import run_llm_service

    async def _combined():
        await asyncio.gather(
            serve_async(),
            run_llm_service(on_status=state.set_llm_status, retry_event=state.llm_retry_event),
        )

    print("== Starting LegoBot GUI + LLM service on http://localhost:8000 ==")
    print("Ctrl+C to stop.")
    try:
        asyncio.run(_combined())
    except KeyboardInterrupt:
        print("\n[Stopped]")


def main():
    parser = argparse.ArgumentParser(description="LegoBot entry point")
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="build + push android_project to the phone via adb, then exit",
    )
    args = parser.parse_args()

    if args.deploy:
        _run_deploy()
        return

    _run_gui_and_llm()


if __name__ == "__main__":
    main()
