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
        CommandBus.post("probe :$PORT ready — /health /ble/scan /cam/test /mic/test /command")
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

    fun receive(cmd: String, args: JSONObject, hub: String): JSONObject {
        val entry = JSONObject()
        entry.put("cmd", cmd)
        entry.put("args", args)
        entry.put("hub", hub)
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

    override fun serve(session: IHTTPSession): Response {
        val result: JSONObject = try {
            when (session.uri.trimEnd('/')) {
                "/health" -> health()
                "/ble/scan" -> runBlocking { bleScanner.scan(intParam(session, "seconds", 4)) }
                "/cam/test" -> runBlocking { cameraProbe.captureOne() }
                "/mic/test" -> runBlocking { micProbe.recordFor(intParam(session, "seconds", 2)) }
                "/command" -> command(session)
                "/command/log" -> JSONObject().put("status", "ok").put("commands", commandLog.recent())
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
     * Dummy command receiver -- same {"cmd", "args", "hub"} body shape as
     * relay_server.py's Command model, POSTed the same way pc_server.py's
     * /command would eventually be told to reach this phone instead of
     * the Pi. Deliberately does NOT validate cmd against hub_controller's
     * COMMANDS dict or touch any hardware -- the whole point of this
     * stage is proving delivery (PC sent it, phone got it, phone
     * answered) without the robot in the loop yet. Swapping the body of
     * this function for a real dispatch() call later is the only thing
     * that needs to change.
     */
    private fun command(session: IHTTPSession): JSONObject {
        if (session.method != Method.POST) {
            return JSONObject().put("status", "error").put("message", "/command requires POST")
        }
        val files = HashMap<String, String>()
        session.parseBody(files)
        val bodyStr = files["postData"] ?: "{}"
        val body = JSONObject(bodyStr)

        val cmd = body.optString("cmd", "")
        if (cmd.isEmpty()) {
            return JSONObject().put("status", "error").put("message", "missing 'cmd' in request body")
        }
        val args = body.optJSONObject("args") ?: JSONObject()
        val hub = body.optString("hub", "front")

        val entry = commandLog.receive(cmd, args, hub)
        CommandBus.post("› $cmd  hub=$hub  $args")
        CommandBus.post("  state: idle — probe stage, no hub attached")

        val out = JSONObject()
        out.put("status", "ok")
        out.put("cmd", cmd)
        out.put("args", args)
        out.put("hub", hub)
        out.put("received_at", entry.get("received_at"))
        out.put("note", "probe stage -- logged only, no hub attached yet")
        return out
    }

    private fun intParam(session: IHTTPSession, name: String, default: Int): Int {
        return session.parameters[name]?.firstOrNull()?.toIntOrNull() ?: default
    }
}
