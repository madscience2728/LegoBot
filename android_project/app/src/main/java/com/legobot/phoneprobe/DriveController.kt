package com.legobot.phoneprobe

import org.json.JSONObject
import kotlin.math.abs

/**
 * Semantic drive commands -- the actual surface the future LLM layer
 * will call (go_forward/go_back/turn_left/turn_right/tilt_head_up/
 * tilt_head_down/tilt_head_center), translated down into WheelMap-
 * addressed per-wheel HubConnector calls. Kept separate from
 * ProbeService's raw /part/set_speed endpoint (still there, still
 * useful for manual per-wheel calibration) since this is a genuinely
 * different layer: the /part endpoints say WHICH MOTOR to move, this
 * says WHAT THE ROBOT SHOULD DO.
 *
 * Two things below are hand-confirmed choices, not assumptions -- same
 * "ask, don't guess" reasoning as the wheel invert flags and the LED
 * mode fix:
 *
 *   - Turns are a pivot in place: the left wheel reverses while the
 *     right wheel drives forward (turn_left), or vice versa (turn_right)
 *     -- NOT an arc turn where one side just stops. (2-wheel-drive
 *     rebuild -- there's only one wheel per side now, not two.)
 *   - Head tilt is three fixed absolute presets via APOS, not a
 *     relative nudge: tilt_head_up is -45 degrees, tilt_head_down is
 *     +45. tilt_head_center/forward is -7, NOT 0 -- the mount's weight
 *     tilts it forward a bit at rest, so true "looking level" isn't the
 *     servo's zero point; -7 is the confirmed compensation for that
 *     droop. Only center needed this correction -- up/down are the
 *     original full-extent symmetric targets, unchanged. speed/duration
 *     are deliberately NOT parameters for tilt (unlike the wheel
 *     commands) -- there's no "how far" to specify beyond the fixed
 *     target, since this is an absolute bounded move via gotoApos, not
 *     an open-loop timed run like the wheel commands are.
 */
class DriveController(private val hubs: Map<String, HubConnector>) {

    private fun connectorFor(part: String): Pair<HubConnector, WheelMap.Wiring>? {
        val wiring = WheelMap.PARTS[part] ?: return null
        val connector = hubs[wiring.hub] ?: return null
        return connector to wiring
    }

    /** Drives both wheels the same physical direction for durationS
     * seconds -- go_forward and go_back share this, since the only
     * difference between them is the sign of speed. */
    private suspend fun driveStraight(speed: Double, durationS: Double): JSONObject {
        val wheels = JSONObject()
        for (part in WheelMap.WHEELS) {
            val (connector, wiring) = connectorFor(part) ?: continue
            wheels.put(part, connector.driveForTime(wiring.port, speed, durationS, invert = wiring.invert))
        }
        return JSONObject().put("status", "ok").put("wheels", wheels)
    }

    suspend fun goForward(speed: Double, durationS: Double): JSONObject =
        driveStraight(abs(speed).coerceIn(0.0, 1.0), durationS.coerceIn(0.0, 5.0))

    suspend fun goBack(speed: Double, durationS: Double): JSONObject =
        driveStraight(-abs(speed).coerceIn(0.0, 1.0), durationS.coerceIn(0.0, 5.0))

    /** Pivot turn -- confirmed kinematics, see class doc. leftSign=-1
     * means "left side gets -speed (reverse), right side gets +speed
     * (forward)" -- that's turn_left. leftSign=+1 mirrors it for
     * turn_right. Each wheel's `invert` is still applied on top (via
     * HubConnector.driveForTime), same as every other drive command --
     * leftSign*speed here is always the intended PHYSICAL direction,
     * not the raw wire sign. */
    private suspend fun pivotTurn(leftSign: Int, speed: Double, durationS: Double): JSONObject {
        val clampedSpeed = abs(speed).coerceIn(0.0, 1.0)
        val clampedDuration = durationS.coerceIn(0.0, 5.0)
        val wheels = JSONObject()
        connectorFor("left_wheel")?.let { (connector, wiring) ->
            wheels.put("left_wheel", connector.driveForTime(wiring.port, leftSign * clampedSpeed, clampedDuration, invert = wiring.invert))
        }
        connectorFor("right_wheel")?.let { (connector, wiring) ->
            wheels.put("right_wheel", connector.driveForTime(wiring.port, -leftSign * clampedSpeed, clampedDuration, invert = wiring.invert))
        }
        return JSONObject().put("status", "ok").put("wheels", wheels)
    }

    suspend fun turnLeft(speed: Double, durationS: Double): JSONObject = pivotTurn(leftSign = -1, speed, durationS)
    suspend fun turnRight(speed: Double, durationS: Double): JSONObject = pivotTurn(leftSign = 1, speed, durationS)

    /** direction: "up" (-45), "down" (+45), or "center"/"forward" (-7) --
     * anything else is an error, not a silent fallback to center. */
    suspend fun tiltHead(direction: String): JSONObject {
        val target = when (direction.lowercase()) {
            "up" -> -45.0
            "down" -> 45.0
            "center", "forward" -> -7.0
            else -> return JSONObject().put("status", "error")
                .put("message", "unknown tilt direction '$direction', expected up/down/center")
        }
        val wiring = WheelMap.PARTS["head_tilt_servo"]
            ?: return JSONObject().put("status", "error").put("message", "head_tilt_servo not in WheelMap")
        val connector = hubs[wiring.hub]
            ?: return JSONObject().put("status", "error").put("message", "no HubConnector for hub '${wiring.hub}'")
        return connector.gotoApos(wiring.port, target)
    }
}
