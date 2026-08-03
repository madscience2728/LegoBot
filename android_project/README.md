# LegoBot Phone Probe

Proves BLE scan, camera capture, mic recording, and an on-phone HTTP
server all work on your Galaxy S9+ *before* any of it gets wired into
`hub_controller.py` / `media_relay.py`. Nothing here talks to the
robot.

## Why this exists

`scripts/deploy.py` (the LegoBot one) targets a Raspberry Pi over SSH.
Pi's been unstable, so the plan is to move the "always-on device near
the robot" role to the spare S9+. Two of the Pi-side dependencies
(`bleak`/BlueZ for BLE, `sounddevice`/PortAudio + `cv2` for mic/camera)
don't have an equivalent on stock Android -- this is a from-scratch
Android app using the native BLE/Camera2/AudioRecord APIs instead, to
find out whether each piece is even viable before rebuilding anything
around it.

## One-time setup

**On the PC**, you need:
- A JDK (already required by the checked-in Gradle wrapper)
- `adb` on PATH -- comes with Android SDK platform-tools
- **Easiest path**: install Android Studio and open this folder as a
  project once. It downloads the correct SDK/build-tools automatically
  and gives you a "Run" button as an alternative to `deploy.py`.
- **Command-line only**: install the [SDK command-line
  tools](https://developer.android.com/studio#command-tools), then
  `sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0"`
  and make sure `platform-tools` (which contains `adb`) is on PATH.

**On the phone**:
1. Settings > About phone > tap "Build number" 7 times -> enables Developer options
2. Settings > Developer options > enable USB debugging
3. Plug in via USB once, accept the "Allow USB debugging?" prompt (check "always allow")
4. Make sure the phone's WiFi is on the same network as your PC (the probe endpoints are plain HTTP, no port forwarding set up)

## Build + install + run

```bash
python3 deploy.py
```

This runs `gradlew assembleDebug`, installs the APK over ADB, and
launches the app. On first launch, grant the camera/mic/Bluetooth
permission prompts. The app's screen then shows the phone's current IP
and the four endpoint URLs.

## Test each piece from the PC

```bash
python3 scripts/probe_client.py <phone-ip>
```

Hits `/health`, `/ble/scan`, `/cam/test`, `/mic/test` in turn and
prints each JSON response. A `"status": "error"` on any one tells you
which piece to dig into -- e.g. `/ble/scan` returning `device_count: 0`
everywhere means either Bluetooth is off or nothing nearby is
advertising, `/cam/test` failing on `onConfigureFailed` usually means a
permission or hardware-busy issue, etc.

## Known rough edges going in

- **Background survival**: Android will kill the service if the phone
  sleeps, unless you disable battery optimization for this app
  (Settings > Apps > LegoBot Phone Probe > Battery > Unrestricted).
  Fine for proving things work at a desk; matters once this needs to
  run unattended near the robot.
- **BLE scan proves radio + permissions, not a GATT connect.** Talking
  to the actual hubs (`Control+ Hub` / `Daril`) needs
  `BluetoothGatt`/`BluetoothGattCallback` code that doesn't exist yet --
  intentionally deferred until scan is proven solid.
- **NanoHTTPD is plain HTTP**, not WebSocket. `media_relay.py`'s wire
  format (binary WebSocket frames) will need a different server layer
  (e.g. Ktor) once you're past single-shot capture and into actual
  streaming.
