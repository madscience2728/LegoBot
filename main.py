"""
main.py -- single entry point for LegoBot, run from the PC.

Usage:
    python3 main.py           # start the GUI dashboard (gui/server.py):
                               # camera, mic, health, BLE, timing, and the
                               # command console, all at
                               # http://localhost:8000. Blocks until Ctrl+C.

    python3 main.py --deploy  # build + push the Android probe app to the
                               # phone (android_project/deploy.py), then
                               # exit. Doesn't touch the GUI at all -- a
                               # one-off maintenance action, same
                               # separation the old Pi-era main.py (see
                               # old/main.py) drew between running the
                               # thing and pushing code to it.

What this does NOT do (yet):
    Route commands anywhere beyond what the GUI's Command Console sends
    to the phone's dummy /command endpoint. The old Pi-era routing logic
    (old/src/lego_brain/robot_client.py, pc_server.py) is archived under
    old/ and isn't wired up here -- there's no robot in the loop at this
    stage, on purpose.
"""
import argparse


def _run_deploy():
    print("== Deploying android_project to the phone ==")
    from android_project.deploy import main as deploy_main
    deploy_main()


def _run_gui():
    from gui.server import main as run_gui
    print("== Starting LegoBot GUI on http://localhost:8000 (Ctrl+C to stop) ==")
    run_gui()


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

    _run_gui()


if __name__ == "__main__":
    main()
