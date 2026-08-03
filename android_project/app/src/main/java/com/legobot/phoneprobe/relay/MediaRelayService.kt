package com.legobot.phoneprobe.relay

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.legobot.phoneprobe.CommandBus

private const val CHANNEL_ID = "media_relay_service"
private const val NOTIFICATION_ID = 2

/**
 * The real target this whole probe phase was building toward: continuous,
 * non-blocking camera + mic capture, relayed live over the same wire
 * protocol lego_pi/media_relay.py already defines. Nothing about hub
 * control lives here on purpose -- this proves sensor data end-to-end
 * first, hub wiring is a deliberately separate later step.
 */
class MediaRelayService : Service() {

    private val cameraStream by lazy { CameraStream(applicationContext) }
    private val micStream by lazy { MicStream(applicationContext) }
    private val relayServer = MediaRelayServer()

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildNotification())
        cameraStream.start()
        micStream.start()
        relayServer.start()
        CommandBus.post("media relay :8001 ready — ws /media")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        relayServer.stop()
        cameraStream.stop()
        micStream.stop()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "LegoBot Media Relay", NotificationManager.IMPORTANCE_LOW
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification() =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("LegoBot Media Relay")
            .setContentText("Streaming camera + mic on ws://<this-phone>:8001/media")
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setOngoing(true)
            .build()
}
