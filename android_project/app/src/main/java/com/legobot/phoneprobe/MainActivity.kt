package com.legobot.phoneprobe

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.text.format.Formatter
import android.view.View
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.view.updatePadding

/**
 * Single screen: request every permission the probe endpoints need,
 * start ProbeService + MediaRelayService, then get out of the way.
 *
 * Layout is IP header (top) / emoji face (center, placeholder for a
 * future robot-state expression) / scrolling command console (bottom).
 * No visible chrome beyond that -- see themes.xml's docstring for why
 * the window is edge-to-edge with transparent system bars: the previous
 * version's opaque nav bar looked like it was overriding gesture
 * navigation, when really it was just an un-transparent bar sitting on
 * top of it.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var ipText: TextView
    private lateinit var emojiFace: TextView
    private lateinit var consoleText: TextView
    private lateinit var consoleScroll: ScrollView

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
        if (results.values.all { it }) {
            startServices()
            updateIpText()
        } else {
            val denied = results.filterValues { !it }.keys.joinToString(", ")
            log("permission denied: $denied")
            log("grant manually in Settings > Apps > LegoBot Phone Probe > Permissions, then relaunch")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setUpEdgeToEdgeWindow()
        setContentView(R.layout.activity_main)

        ipText = findViewById(R.id.ipText)
        emojiFace = findViewById(R.id.emojiFace)
        consoleText = findViewById(R.id.consoleText)
        consoleScroll = findViewById(R.id.consoleScroll)

        // Push the IP header below the status bar / punch-hole area and
        // the console above the gesture nav strip, now that the window
        // itself draws full-screen behind both.
        val consoleContainer = findViewById<View>(R.id.consoleContainer)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById<View>(R.id.rootLayout)) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            ipText.updatePadding(top = bars.top + 16)
            consoleContainer.updatePadding(bottom = bars.bottom)
            insets
        }

        log("probe :8765 ready — /health /ble/scan /cam/test /mic/test /command")
        log("media relay :8001 ready — ws /media")

        val missing = requiredPermissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) {
            startServices()
            updateIpText()
        } else {
            permissionLauncher.launch(missing.toTypedArray())
        }
    }

    override fun onStart() {
        super.onStart()
        // Only listen while actually visible -- CommandBus is a plain
        // singleton with no lifecycle awareness of its own.
        CommandBus.subscribe { line -> log(line) }
    }

    override fun onStop() {
        CommandBus.unsubscribe()
        super.onStop()
    }

    private fun setUpEdgeToEdgeWindow() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.TRANSPARENT
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }
    }

    private fun startServices() {
        ContextCompat.startForegroundService(this, Intent(this, ProbeService::class.java))
        ContextCompat.startForegroundService(
            this, Intent(this, com.legobot.phoneprobe.relay.MediaRelayService::class.java)
        )
        log("services started")
    }

    private fun updateIpText() {
        val wifi = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
        val ip = try {
            @Suppress("DEPRECATION")
            Formatter.formatIpAddress(wifi.connectionInfo.ipAddress)
        } catch (e: Exception) {
            "no wifi"
        }
        ipText.text = ip
    }

    /** Hook for later: once there's a real state machine on the phone,
     * call this from wherever robot state changes to reflect it here
     * instead of the static placeholder. */
    @Suppress("unused")
    private fun setFace(emoji: String) {
        emojiFace.text = emoji
    }

    private fun log(line: String) {
        consoleText.append((if (consoleText.text.isEmpty()) "" else "\n") + line)
        consoleScroll.post { consoleScroll.fullScroll(View.FOCUS_DOWN) }
    }
}
