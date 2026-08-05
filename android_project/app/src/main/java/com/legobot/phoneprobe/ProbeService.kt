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
                "/hub/disconnect /hub/status /hub/describe_port /hub/led /part/set_speed /part/stop"
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
     * "stop" and "set_speed" now really dispatch to HubConnector (see
     * below) -- the first two commands in this vocabulary to graduate
     * out of the dummy log-only path, same way describe_port/led did by
     * getting their own dedicated endpoints. Everything else here
     * (read_angle, preset_zero, read_apos, home_angle, set_position,
     * goto_angle) still needs encoder-subscription infrastructure this
     * class doesn't have yet, so those stay logged-only for now.
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

    private fun intParam(session: IHTTPSession, name: String, default: Int): Int {
        return session.parameters[name]?.firstOrNull()?.toIntOrNull() ?: default
    }
}
