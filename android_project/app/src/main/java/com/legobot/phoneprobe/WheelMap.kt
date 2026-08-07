package com.legobot.phoneprobe

/**
 * Physical wiring map: robot part name -> which hub + port it's actually
 * plugged into, plus whether that port's motor needs its speed negated
 * so "positive speed" means the same physical direction on both wheels.
 * Hand-confirmed against the real robot via the GUI's Wheels panel, not
 * assumed -- same "ask the actual device/build, don't guess" principle
 * the rest of this project follows (describe_port, the LED mode fix).
 *
 * Rebuilt for the chassis rebuild (2-wheel-drive instead of the old
 * 4-wheel skid-steer, single hub instead of front+rear). The port
 * assignments below are NOT a relabeling of the old ones -- confirmed
 * fresh after the rebuild: port C, which used to be front_right_wheel
 * (inverted), is now the LEFT wheel and does NOT need inverting; port A,
 * which used to be rear_right_wheel (inverted), is still the RIGHT wheel
 * and still needs inverting. Port D is unused on this build. Port B is
 * still the head-tilt servo, same as before.
 */
object WheelMap {

    data class Wiring(val hub: String, val port: String, val invert: Boolean = false)

    val PARTS: Map<String, Wiring> = mapOf(
        "left_wheel" to Wiring(hub = "front", port = "C", invert = false),
        "right_wheel" to Wiring(hub = "front", port = "A", invert = true),
        "head_tilt_servo" to Wiring(hub = "front", port = "B", invert = false),
    )

    /** Both drive wheels, excluding head_tilt_servo -- the set a "stop
     * everything" or "drive forward" convenience would iterate. */
    val WHEELS: List<String> = PARTS.keys.filter { it != "head_tilt_servo" }
}
