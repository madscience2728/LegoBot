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
import org.json.JSONObject

private const val CHANNEL_ID = "probe_service"
private const val NOTIFICATION_ID = 1
private const val PORT = 8765

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

private class ProbeServer(private val context: android.content.Context) : NanoHTTPD(PORT) {

    private val bleScanner = BleScanner(context)
    private val cameraProbe = CameraProbe(context)
    private val micProbe = MicProbe(context)

    override fun serve(session: IHTTPSession): Response {
        val result: JSONObject = try {
            when (session.uri.trimEnd('/')) {
                "/health" -> health()
                "/ble/scan" -> runBlocking { bleScanner.scan(intParam(session, "seconds", 4)) }
                "/cam/test" -> runBlocking { cameraProbe.captureOne() }
                "/mic/test" -> runBlocking { micProbe.recordFor(intParam(session, "seconds", 2)) }
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
        return out
    }

    private fun intParam(session: IHTTPSession, name: String, default: Int): Int {
        return session.parameters[name]?.firstOrNull()?.toIntOrNull() ?: default
    }
}
