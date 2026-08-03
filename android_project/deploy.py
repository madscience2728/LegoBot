#!/usr/bin/env python3
"""
deploy.py -- build the probe app, push it to the connected phone, install,
and launch it. Can be run from anywhere -- it cd's into its own folder
first, so `python deploy.py` and `python full/path/to/deploy.py` both work.

One-time phone setup (see the walkthrough for detail):
    Settings > About phone > tap Build number x7 -> Developer options
    Settings > Developer options > enable USB debugging
    Plug in via USB once, accept the RSA fingerprint prompt

Requires `adb` on PATH (Android SDK platform-tools) and a JDK (used by
the Gradle wrapper already checked into this project).
"""
import subprocess
import sys
import glob
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PACKAGE = "com.legobot.phoneprobe"
LAUNCHER_ACTIVITY = f"{PACKAGE}/.MainActivity"
GRADLEW = os.path.join(".", "gradlew.bat" if os.name == "nt" else "gradlew")


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}")
        sys.exit(result.returncode)


def main():
    # Everything below uses relative paths (.\gradlew.bat, app/build/...) --
    # this makes those resolve correctly no matter what directory you were
    # in when you ran `python deploy.py`.
    os.chdir(_SCRIPT_DIR)

    run([GRADLEW, "assembleDebug"])

    apks = glob.glob("app/build/outputs/apk/debug/*.apk")
    if not apks:
        print("No APK found after build.")
        sys.exit(1)
    apk_path = apks[0]
    print(f"Built: {apk_path}")

    devices = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    connected = [l for l in devices.splitlines()[1:] if l.strip().endswith("device")]
    if not connected:
        print("No authorized ADB device found.")
        print("Check: USB cable plugged in, USB debugging enabled, RSA prompt accepted on the phone.")
        sys.exit(1)

    run(["adb", "install", "-r", apk_path])
    run(["adb", "shell", "am", "start", "-n", LAUNCHER_ACTIVITY])

    print()
    print("Installed and launched. On the phone, grant the permission prompts,")
    print("then check the on-screen status text for the phone's IP address and")
    print("the four endpoint URLs to hit from this PC.")
    print()
    print("Quick test once you have the IP: python3 scripts/probe_client.py <phone-ip>")


if __name__ == "__main__":
    main()
