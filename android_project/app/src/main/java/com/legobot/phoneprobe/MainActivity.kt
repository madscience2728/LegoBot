package com.legobot.phoneprobe

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.text.format.Formatter
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * Single screen: request every permission the probe endpoints need,
 * then start ProbeService. There's deliberately no "control the robot"
 * UI here -- this app's only job is proving BLE/camera/mic/server work
 * on this specific phone before any of that gets wired to hub_controller.
 */
class MainActivity : AppCompatActivity() {

    private val requiredPermissions: Array<String>
        get() {
            val perms = mutableListOf(
                Manifest.permission.CAMERA,
                Manifest.permission.RECORD_AUDIO,
            )
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                perms += Manifest.permission.BLUETOOTH_SCAN
                perms += Manifest.permission.BLUETOOTH_CONNECT
            } else {
                perms += Manifest.permission.ACCESS_FINE_LOCATION
            }
            return perms.toTypedArray()
        }

    private val permissionLauncher = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.RequestMultiplePermissions()
    ) { results ->
        val status = findViewById<TextView>(R.id.statusText)
        if (results.values.all { it }) {
            startProbeService()
            status.text = buildStatusText("Service running.")
        } else {
            val denied = results.filterValues { !it }.keys.joinToString(", ")
            status.text = "Denied: $denied\n\nOpen Settings > Apps > LegoBot Phone Probe > Permissions to grant manually, then relaunch."
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val missing = requiredPermissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) {
            startProbeService()
            findViewById<TextView>(R.id.statusText).text = buildStatusText("Service running.")
        } else {
            permissionLauncher.launch(missing.toTypedArray())
        }
    }

    private fun startProbeService() {
        val intent = Intent(this, ProbeService::class.java)
        ContextCompat.startForegroundService(this, intent)
    }

    private fun buildStatusText(extra: String): String {
        val wifi = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
        val ip = try {
            Formatter.formatIpAddress(wifi.connectionInfo.ipAddress)
        } catch (e: Exception) {
            "unknown -- check phone's WiFi settings"
        }
        return "$extra\n\nHit it from your PC (same WiFi network):\n" +
            "  http://$ip:8765/health\n" +
            "  http://$ip:8765/ble/scan\n" +
            "  http://$ip:8765/cam/test\n" +
            "  http://$ip:8765/mic/test\n\n" +
            "Keep this app open / the phone unlocked while testing -- Android will\n" +
            "kill the background service if the phone sleeps unless you've also\n" +
            "disabled battery optimization for this app."
    }
}
