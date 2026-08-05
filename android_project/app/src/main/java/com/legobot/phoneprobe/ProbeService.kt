package com.legobot.phoneprobe

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import fi.iki.elonen.NanoHTTPD
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import java.util.LinkedList

private const val CHANNEL_ID = "probe_service"
private const val NOTIFICATION_ID = 1
private const val PORT = 8765
private const val COMMAND_LOG_CAPACITY = 50

// Same vocabulary as old/src/lego_control/hub_controller.py's COMMANDS
// dict, and gui/server.py's KNOWN_COMMANDS on the PC -- duplicated in
// all three since Kotlin/Python can't share source here.
//
// "stop" and "set_speed" now really dispatch to HubConnector (see
// command() below); the rest still just validate the cmd/hub names and
// log, so a typo'd cmd never silently round-trips as "ok" while their
// own dispatch is still unbuilt.
private val KNOWN_COMMANDS = setOf(
    "stop", "set_speed", "read_angle", "preset_zero",
    "read_apos", "home_angle", "set_position", "goto_angle",
)
private val KNOWN_HUBS = setOf("front", "rear")

/**
 * Runs ON THE PHONE -- this is the phone-side equivalent of
 * lego_pi/relay_server.py + media_relay.py, except right now it only
 * proves the pieces work rather than actually driving anything.
 * Deliberately one process/one service, same "one lifecycle" reasoning
 * as lego_pi/__main__.py's docstring: nothing here needs to be split
 * into multiple services before there's an actual robot in the loop.
 */
class ProbeService : Service() {

    private var server: ProbeServer? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildNotification())
        server = ProbeServer(applicationContext).also { it.start(NanoHTTPD.SOCKET_READ_TIMEOUT, false) }
        CommandBus.post(
            "probe :$PORT ready — /health /ble/scan /cam/test /mic/test /command /hub/connect " +
                "/hub/disconnect /hub/status /hub/describe_port /hub/led /part/set_speed /part/stop " +
                "/face/set /face/status /drive"
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    override fun onDestroy() {
        server?.stop()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "LegoBot Probe", NotificationManager.IMPORTANCE_LOW
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification() =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("LegoBot Phone Probe")
            .setContentText("Serving probe endpoints on port $PORT")
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setOngoing(true)
            .build()
}

/**
 * Holds the same shape relay_server.py's Command model does (cmd/args/hub)
 * so nothing on the PC side needs to change when this eventually stops
 * being a dummy -- only what happens INSIDE receive() changes once
 * hub_controller-equivalent logic exists on Android. Right now it does
 * the absolute minimum: remember it happened, echo it back.
 */
private class CommandLog {
    private val lock = Any()
    private val entries = LinkedList<JSONObject>()

    fun receive(cmd: String, args: JSONObject, hub: String, valid: Boolean): JSONObject {
        val entry = JSONObject()
        entry.put("cmd", cmd)
        entry.put("args", args)
        entry.put("hub", hub)
        entry.put("valid", valid)
        entry.put("received_at", System.currentTimeMillis() / 1000.0)
        synchronized(lock) {
            entries.addFirst(entry)
            while (entries.size > COMMAND_LOG_CAPACITY) entries.removeLast()
        }
        return entry
    }

    fun count(): Int = synchronized(lock) { entries.size }

    fun recent(): JSONArray {
        val out = JSONArray()
        synchronized(lock) { entries.forEach { out.put(it) } }
        return out
    }
}

private class ProbeServer(private val context: android.content.Context) : NanoHTTPD(PORT) {

    private val bleScanner = BleScanner(context)
    private val cameraProbe = CameraProbe(context)
    private val micProbe = MicProbe(context)
    private val commandLog = CommandLog()

    // Names match old/src/lego_pi/relay_server.py's FRONT_HUB_NAME /
    // REAR_HUB_NAME defaults exactly -- these are the actual BLE
    // advertised names the two physical hubs use, not placeholders.
    private val hubs = mapOf(
        "front" to HubConnector(context, hubName = "Control+ Hub", label = "front"),
        "rear" to HubConnector(context, hubName = "Daril", label = "rear"),
    )

    // The actual command surface a future LLM's tool calls will use --
    // see DriveController's own doc for the turn/tilt kinematics this
    // rides on.
    private val driveController = DriveController(hubs)

    override fun serve(session: IHTTPSession): Response {
        val result: JSONObject = try {
            when (session.uri.trimEnd('/')) {
                "/health" -> health()
                "/ble/scan" -> runBlocking { bleScanner.scan(intParam(session, "seconds", 4)) }
                "/cam/test" -> runBlocking { cameraProbe.captureOne() }
                "/mic/test" -> runBlocking { micProbe.recordFor(intParam(session, "seconds", 2)) }
                "/command" -> command(session)
                "/command/log" -> JSONObject().put("status", "ok").put("commands", commandLog.recent())
                "/hub/connect" -> hubConnect(session)
                "/hub/disconnect" -> hubDisconnect(session)
                "/hub/status" -> hubStatus()
                "/hub/describe_port" -> hubDescribePort(session)
                "/hub/led" -> hubLed(session)
                "/part/set_speed" -> partSetSpeed(session)
                "/part/stop" -> partStop(session)
                "/face/set" -> faceSet(session)
                "/face/status" -> faceStatus()
                "/drive" -> drive(session)
                else -> JSONObject().put("status", "error").put("message", "No such endpoint: ${session.uri}")
            }
        } catch (e: Exception) {
            JSONObject().put("status", "error").put("message", "Unhandled exception: ${e.message}")
        }
        return newFixedLengthResponse(Response.Status.OK, "application/json", result.toString(2))
    }

    private fun health(): JSONObject {
        val out = JSONObject()
        out.put("status", "ok")
        out.put("device", Build.MODEL)
        out.put("android_sdk", Build.VERSION.SDK_INT)
        out.put("commands_received", commandLog.count())
        return out
    }

    /**
     * Command receiver -- same {"cmd", "args", "hub"} body shape as
     * relay_server.py's Command model, POSTed the same way pc_server.py's
     * /command would eventually be told to reach this phone instead of
     * the Pi. Validates cmd/hub against the same vocabulary
     * hub_controller.py's COMMANDS dict defines.
     *
     * "stop", "set_speed", "preset_zero", "read_apos", and "home_angle"
     * now really dispatch to HubConnector (see below) -- the head-tilt
     * servo's fine-control trio (preset_zero/read_apos/home_angle) uses
     * the motor's magnet-based APOS, not the relative POS counter, so
     * its numbers survive reconnects and don't need recalibrating every
     * session -- just once, with the servo held at the position that
     * should read 0. "read_angle", "set_position", and "goto_angle"
     * (POS-based) still need their own encoder-subscription work and
     * stay logged-only.
     */
    private fun command(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/command requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val bodyStr = files["postData"] ?: "{}"
        val body = JSONObject(bodyStr)

        val cmd = body.optString("cmd", "").trim()
        if (cmd.isEmpty()) {
            return JSONObject().put("status", "error").put("message", "missing 'cmd' in request body")
        }
        val args = body.optJSONObject("args") ?: JSONObject()
        val hub = body.optString("hub", "front").lowercase()

        if (cmd !in KNOWN_COMMANDS) {
            commandLog.receive(cmd, args, hub, valid = false)
            CommandBus.post("› $cmd  hub=$hub  $args")
            CommandBus.post("  rejected: unknown cmd (expected one of ${KNOWN_COMMANDS.sorted().joinToString(", ")})")
            return JSONObject().put("status", "error")
                .put("message", "unknown command '$cmd', expected one of ${KNOWN_COMMANDS.sorted()}")
        }
        if (hub !in KNOWN_HUBS) {
            commandLog.receive(cmd, args, hub, valid = false)
            CommandBus.post("› $cmd  hub=$hub  $args")
            CommandBus.post("  rejected: unknown hub (expected one of ${KNOWN_HUBS.sorted().joinToString(", ")})")
            return JSONObject().put("status", "error")
                .put("message", "unknown hub '$hub', expected one of ${KNOWN_HUBS.sorted()}")
        }

        val entry = commandLog.receive(cmd, args, hub, valid = true)
        CommandBus.post("› $cmd  hub=$hub  $args")

        val connector = hubs[hub]
            ?: return JSONObject().put("status", "error").put("message", "no HubConnector for hub '$hub'")

        val dispatchResult: JSONObject? = when (cmd) {
            "stop" -> runBlocking { connector.stop(args.optString("port", "A")) }
            "set_speed" -> runBlocking {
                connector.setSpeed(
                    args.optString("port", "A"),
                    args.optDouble("speed", 0.0),
                    args.optBoolean("invert", false),
                )
            }
            "preset_zero" -> runBlocking { connector.presetZero(args.optString("port", "A")) }
            "read_apos" -> runBlocking { connector.readApos(args.optString("port", "A")) }
            "home_angle" -> runBlocking {
                connector.gotoApos(
                    args.optString("port", "A"),
                    args.optDouble("target_degrees", 0.0),
                    args.optDouble("speed", 0.4),
                    args.optDouble("max_power", 0.5),
                    args.optBoolean("invert", false),
                )
            }
            else -> null
        }

        if (dispatchResult != null) {
            return JSONObject()
                .put("status", dispatchResult.optString("status", "error"))
                .put("cmd", cmd)
                .put("args", args)
                .put("hub", hub)
                .put("received_at", entry.get("received_at"))
                .put("result", dispatchResult)
        }

        CommandBus.post("  state: idle — probe stage, dispatch not implemented for '$cmd' yet")
        val out = JSONObject()
        out.put("status", "ok")
        out.put("cmd", cmd)
        out.put("args", args)
        out.put("hub", hub)
        out.put("received_at", entry.get("received_at"))
        out.put("note", "probe stage -- logged only, dispatch not implemented for '$cmd' yet")
        return out
    }

    /**
     * Triggers a real BLE GATT connect to one hub (see HubConnector) and
     * blocks until it succeeds, fails, or times out (~15-25s worst case:
     * scan + GATT handshake + subscribe) -- same blocking-request style
     * as /ble/scan and /cam/test already use, so callers should set a
     * generous HTTP timeout, not poll.
     */
    private fun hubConnect(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/hub/connect requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = JSONObject(files["postData"] ?: "{}")
        val hubKey = body.optString("hub", "").lowercase()

        val connector = hubs[hubKey]
            ?: return JSONObject().put("status", "error")
                .put("message", "unknown hub '$hubKey', expected one of ${hubs.keys.sorted()}")

        val result = runBlocking { connector.connect() }
        return JSONObject().put("status", if (connector.state == HubConnState.CONNECTED) "ok" else "error").apply {
            put("hub", result)
        }
    }

    /** Plain teardown -- no scan, no reconnect attempt. Exists because
     * routing "Reconnect" through connect() (which tears down the old
     * link first, then re-scans/re-connects) meant a reconnect that
     * failed partway looked identical to just disconnecting: the old
     * link was already gone, the new one never came up. Splitting
     * disconnect out makes the button's behavior match what it says,
     * instead of quietly doing something else when the retry fails. */
    private fun hubDisconnect(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/hub/disconnect requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = JSONObject(files["postData"] ?: "{}")
        val hubKey = body.optString("hub", "").lowercase()

        val connector = hubs[hubKey]
            ?: return JSONObject().put("status", "error")
                .put("message", "unknown hub '$hubKey', expected one of ${hubs.keys.sorted()}")

        connector.disconnect()
        return JSONObject().put("status", "ok").put("hub", connector.statusJson())
    }

    /** Read-only, non-blocking snapshot of both hubs' current connection
     * state -- doesn't trigger a scan/connect, just reports what the
     * last /hub/connect call (if any) left behind. */
    private fun hubStatus(): JSONObject {
        val out = JSONObject()
        out.put("status", "ok")
        val hubsOut = JSONObject()
        hubs.forEach { (key, connector) -> hubsOut.put(key, connector.statusJson()) }
        out.put("hubs", hubsOut)
        return out
    }

    /** First of the two "actually talk LWP to the hub" commands -- see
     * HubConnector.describePort. Blocking/POST for the same reason
     * hubConnect is: several BLE round-trips (one per port mode), not a
     * single fire-and-forget write. */
    private fun hubDescribePort(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/hub/describe_port requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = JSONObject(files["postData"] ?: "{}")
        val hubKey = body.optString("hub", "").lowercase()
        val port = body.optString("port", "")

        val connector = hubs[hubKey]
            ?: return JSONObject().put("status", "error")
                .put("message", "unknown hub '$hubKey', expected one of ${hubs.keys.sorted()}")

        return runBlocking { connector.describePort(port) }
    }

    /** Second of the two "actually talk LWP to the hub" commands -- see
     * HubConnector.setLedColor / setLedRgb. Accepts EITHER a named
     * "color" (LEGO's Mode-0 palette, e.g. "GREEN" -- the reliable path,
     * confirmed to actually change the physical LED) OR raw "r"/"g"/"b"
     * (Mode-1 direct RGB -- spec-legal, NOT confirmed to render; see
     * HubConnector.setLedRgb's doc comment for why). "color" wins if
     * both are present. */
    private fun hubLed(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/hub/led requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = JSONObject(files["postData"] ?: "{}")
        val hubKey = body.optString("hub", "").lowercase()

        val connector = hubs[hubKey]
            ?: return JSONObject().put("status", "error")
                .put("message", "unknown hub '$hubKey', expected one of ${hubs.keys.sorted()}")

        val colorName = body.optString("color", "").trim().uppercase()
        if (colorName.isNotEmpty()) {
            val colorIndex = Lwp.LedColor.byName[colorName]
                ?: return JSONObject().put("status", "error")
                    .put("message", "unknown color '$colorName', expected one of ${Lwp.LedColor.byName.keys.sorted()}")
            return runBlocking { connector.setLedColor(colorIndex) }
        }

        val r = body.optInt("r", 0)
        val g = body.optInt("g", 0)
        val b = body.optInt("b", 0)
        return runBlocking { connector.setLedRgb(r, g, b) }
    }

    /** Same as /command's set_speed, but addressed by part name (see
     * WheelMap) instead of the caller needing to know which hub+port
     * that part is wired to. Applies WheelMap's confirmed invert flag,
     * so callers of this endpoint always mean "physical direction",
     * not "raw motor sign" -- the calibration this endpoint was
     * originally built to support (see WheelMap's doc comment). */
    private fun partSetSpeed(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/part/set_speed requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = JSONObject(files["postData"] ?: "{}")
        val part = body.optString("part", "")
        val wiring = WheelMap.PARTS[part]
            ?: return JSONObject().put("status", "error")
                .put("message", "unknown part '$part', expected one of ${WheelMap.PARTS.keys.sorted()}")

        val connector = hubs[wiring.hub]
            ?: return JSONObject().put("status", "error").put("message", "no HubConnector for hub '${wiring.hub}'")

        val speed = body.optDouble("speed", 0.0)
        val result = runBlocking { connector.setSpeed(wiring.port, speed, invert = wiring.invert) }
        return JSONObject().put("status", result.optString("status", "error"))
            .put("part", part).put("hub", wiring.hub).put("port", wiring.port).put("result", result)
    }

    private fun partStop(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/part/stop requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = JSONObject(files["postData"] ?: "{}")
        val part = body.optString("part", "")
        val wiring = WheelMap.PARTS[part]
            ?: return JSONObject().put("status", "error")
                .put("message", "unknown part '$part', expected one of ${WheelMap.PARTS.keys.sorted()}")

        val connector = hubs[wiring.hub]
            ?: return JSONObject().put("status", "error").put("message", "no HubConnector for hub '${wiring.hub}'")

        val result = runBlocking { connector.stop(wiring.port) }
        return JSONObject().put("status", result.optString("status", "error"))
            .put("part", part).put("hub", wiring.hub).put("port", wiring.port).put("result", result)
    }

    /** Sets the phone's on-screen emoji face -- the robot's actual
     * visible expression (see FaceBus, and MainActivity's emojiFace
     * TextView it drives), not just a log line. Accepts EITHER
     * "expression" (a known name from FaceBus.EXPRESSIONS -- validated,
     * the reliable path a future LLM's tool-call should use) OR "emoji"
     * (an arbitrary raw string -- unvalidated, for anything the fixed
     * vocabulary doesn't cover yet). "expression" wins if both are
     * given. */
    private fun faceSet(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/face/set requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = JSONObject(files["postData"] ?: "{}")

        val expression = body.optString("expression", "").trim()
        if (expression.isNotEmpty()) {
            val emoji = FaceBus.setExpression(expression)
                ?: return JSONObject().put("status", "error")
                    .put("message", "unknown expression '$expression', expected one of ${FaceBus.EXPRESSIONS.keys.sorted()}")
            CommandBus.post("face -> ${expression.lowercase()} $emoji")
            return JSONObject().put("status", "ok").put("expression", expression.lowercase()).put("emoji", emoji)
        }

        val emoji = body.optString("emoji", "").trim()
        if (emoji.isEmpty()) {
            return JSONObject().put("status", "error").put("message", "provide 'expression' or 'emoji'")
        }
        FaceBus.setEmoji(emoji)
        CommandBus.post("face -> $emoji")
        return JSONObject().put("status", "ok").put("emoji", emoji)
    }

    /** Read-only -- current face plus the known expression vocabulary,
     * so a fresh GUI tab (or a future LLM at session start) can see
     * what's on screen right now and what names it can ask for, without
     * needing to have been listening for the last /face/set. */
    private fun faceStatus(): JSONObject {
        val out = JSONObject()
        out.put("status", "ok")
        out.put("emoji", FaceBus.current)
        out.put("expressions", JSONArray(FaceBus.EXPRESSIONS.keys.sorted()))
        return out
    }

    /** The actual command surface a future LLM's tool calls will use --
     * see DriveController for the turn/tilt kinematics this rides on
     * (pivot-in-place turns, fixed-preset head tilt), both hand-
     * confirmed rather than assumed. Distinct from /part/set_speed's
     * raw per-wheel control: that endpoint says WHICH MOTOR to move,
     * this says WHAT THE ROBOT SHOULD DO.
     *
     * speed is 0.0-1.0 (magnitude only -- direction comes from the
     * command name, e.g. go_forward vs go_back, not from speed's sign);
     * duration_s is 0.0-5.0. Both are clamped, not rejected, on an
     * out-of-range value -- consistent with how every other numeric arg
     * in this codebase (LED r/g/b, wheel speed) has been handled: a
     * slightly-out-of-range tool call still does something reasonable
     * instead of erroring the whole command out. tilt_head_* ignores
     * both -- see DriveController's doc for why. */
    private fun drive(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/drive requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val body = JSONObject(files["postData"] ?: "{}")
        val command = body.optString("command", "").trim().lowercase()
        val speed = body.optDouble("speed", 0.5).let { if (it.isNaN()) 0.5 else it }
        val durationS = body.optDouble("duration_s", 1.0).let { if (it.isNaN()) 1.0 else it }

        return runBlocking {
            when (command) {
                "go_forward" -> driveController.goForward(speed, durationS)
                "go_back" -> driveController.goBack(speed, durationS)
                "turn_left" -> driveController.turnLeft(speed, durationS)
                "turn_right" -> driveController.turnRight(speed, durationS)
                "tilt_head_up" -> driveController.tiltHead("up")
                "tilt_head_down" -> driveController.tiltHead("down")
                "tilt_head_center" -> driveController.tiltHead("center")
                else -> JSONObject().put("status", "error").put(
                    "message",
                    "unknown command '$command', expected one of go_forward, go_back, turn_left, " +
                        "turn_right, tilt_head_up, tilt_head_down, tilt_head_center",
                )
            }
        }
    }

    private fun intParam(session: IHTTPSession, name: String, default: Int): Int {
        return session.parameters[name]?.firstOrNull()?.toIntOrNull() ?: default
    }
}
