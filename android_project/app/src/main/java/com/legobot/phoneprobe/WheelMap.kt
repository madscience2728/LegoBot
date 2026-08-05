package com.legobot.phoneprobe

/**
 * Physical wiring map: robot part name -> which hub + port it's actually
 * plugged into, plus whether that port's motor needs its speed negated
 * to make "positive speed" mean the same physical direction across all
 * four wheels. Both hand-confirmed against the real robot, not assumed --
 * same "ask the actual device/build, don't guess" principle the rest of
 * this project follows (describe_port, the LED mode fix).
 *
 * IMPORTANT: the "front"/"rear" HUB names do NOT correspond to a
 * left/right split of the robot -- front_left_wheel and rear_left_wheel
 * are BOTH on the rear hub, while front_right_wheel and rear_right_wheel
 * are BOTH on the front hub. Front/rear hub naming is just which hub
 * physically sits at which end of the chassis; don't infer wiring from
 * it.
 *
 * invert confirmed by running each wheel individually at low speed and
 * watching which physical direction a positive value produced: the
 * right-side wheels (front_right_wheel, rear_right_wheel) needed
 * negating to agree with the left side; the left side and the head-tilt
 * servo did not. This is exactly the class of thing that must be
 * confirmed by hand rather than assumed symmetric -- see HubConnector's
 * LED mode fix for the same lesson learned the hard way.
 */
object WheelMap {

    data class Wiring(val hub: String, val port: String, val invert: Boolean = false)

    val PARTS: Map<String, Wiring> = mapOf(
        "front_left_wheel" to Wiring(hub = "rear", port = "D", invert = false),
        "front_right_wheel" to Wiring(hub = "front", port = "C", invert = true),
        "rear_left_wheel" to Wiring(hub = "rear", port = "B", invert = false),
        "rear_right_wheel" to Wiring(hub = "front", port = "A", invert = true),
        "head_tilt_servo" to Wiring(hub = "front", port = "B", invert = false),
    )

    /** All four drive wheels, excluding head_tilt_servo -- the set a
     * "stop everything" or "drive forward" convenience would iterate. */
    val WHEELS: List<String> = PARTS.keys.filter { it != "head_tilt_servo" }
}
