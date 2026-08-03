#!/usr/bin/env python3
"""
deploy.py -- build the probe app, push it to the connected phone, install,
and launch it. Can be run from anywhere -- it cd's into its own folder
first, so `python deploy.py` and `python full/path/to/deploy.py` both work.

Tries wireless ADB first (no cable needed), falls back to telling you what
to do if nothing's connected yet.

One-time phone setup (see the walkthrough for detail):
    Settings > About phone > tap Build number x7 -> Developer options
    Settings > Developer options > enable USB debugging
    Plug in via USB once, accept the RSA fingerprint prompt

Wireless ADB setup (do this once per phone reboot -- it resets to
USB-only every time the phone restarts, there's no way around that):
    adb tcpip 5555          (phone must be USB-connected for this one command)
    adb connect <phone-ip>:5555

Requires `adb` on PATH (Android SDK platform-tools) and a JDK (used by
the Gradle wrapper already checked into this project).
"""
import subprocess
import sys
import glob
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Set this to your phone's WiFi IP once you know it (shown on the app's
# own screen after launch, or check Settings > About phone > Status).
# A DHCP reservation on your router keeps this from changing -- otherwise
# you'll need to update it whenever the phone gets a new lease.
PHONE_IP = "192.168.4.127"
PHONE_ADB_PORT = "5555"

PACKAGE = "com.legobot.phoneprobe"
LAUNCHER_ACTIVITY = f"{PACKAGE}/.MainActivity"
GRADLEW = os.path.join(".", "gradlew.bat" if os.name == "nt" else "gradlew")


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}")
        sys.exit(result.returncode)


def connected_devices():
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines()[1:] if l.strip().endswith("device")]


def ensure_device_connected():
    if connected_devices():
        return  # already connected, USB or wireless -- nothing to do

    # Nothing connected yet -- try wireless ADB before giving up. This
    # only succeeds if the phone is ALREADY in tcpip mode from a previous
    # `adb tcpip 5555` -- it can't bootstrap a first-time connection or
    # recover after a phone reboot, since that always resets it to
    # USB-only. This is just a quiet "maybe it's already listening"
    # attempt, not a substitute for the real fallback below.
    subprocess.run(
        ["adb", "connect", f"{PHONE_IP}:{PHONE_ADB_PORT}"],
        capture_output=True, text=True
    )

    if connected_devices():
        print(f"Connected wirelessly to {PHONE_IP}:{PHONE_ADB_PORT}.")
        return

    print("No ADB device connected (checked USB and wireless).")
    print(f"Tried connecting wirelessly to {PHONE_IP}:{PHONE_ADB_PORT} -- no response.")
    print()
    print("If the phone rebooted recently, wireless ADB needs to be re-armed over USB:")
    print("  1. Plug the phone in via USB")
    print("  2. adb tcpip 5555")
    print(f"  3. adb connect {PHONE_IP}:{PHONE_ADB_PORT}")
    print("  4. Unplug -- then re-run this script")
    sys.exit(1)


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

    ensure_device_connected()

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
