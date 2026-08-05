package com.legobot.phoneprobe

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import kotlinx.coroutines.delay
import org.json.JSONArray
import org.json.JSONObject

/**
 * Proof-of-life for BLE central mode. This is the piece that
 * lego_control/hub_controller.py needs on Android in place of bleak/
 * BlueZ -- if this can see nearby BLE devices, the phone's radio +
 * permissions are usable and the only remaining work is a real GATT
 * connect/write (see notes in the class doc for hub_controller.py's
 * equivalent) once we're ready to touch the actual hubs.
 *
 * Deliberately does NOT try to connect to anything yet -- scanning is
 * the lowest-risk BLE operation there is (no pairing, no GATT, no
 * hub-specific behavior), which is exactly the point of this probe
 * phase: prove the radio + permissions work before adding real
 * complexity on top.
 */
class BleScanner(private val context: Context) {

    @SuppressLint("MissingPermission") // caller (ProbeService) verifies permissions first
    suspend fun scan(seconds: Int): JSONObject = BleScanLock.withLock {
        val btManager = context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        val adapter = btManager.adapter
            ?: return@withLock errorResult("No Bluetooth adapter on this device.")
        if (!adapter.isEnabled) {
            return@withLock errorResult("Bluetooth is off -- enable it in the phone's quick settings.")
        }
        val scanner = adapter.bluetoothLeScanner
            ?: return@withLock errorResult("BluetoothLeScanner unavailable (adapter busy or off).")

        val found = LinkedHashMap<String, ScanResult>() // address -> latest result, de-duped
        var failureCode: Int? = null
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                found[result.device.address] = result
            }
            override fun onScanFailed(errorCode: Int) {
                // Previously silently discarded (bug) -- a real failure here
                // (e.g. SCAN_FAILED_ALREADY_STARTED from two scans racing on
                // the same radio) used to come back looking identical to
                // "scanned fine, saw nothing," which is exactly the kind of
                // gap this whole probe stage exists to close.
                failureCode = errorCode
            }
        }

        scanner.startScan(callback)
        delay(seconds * 1000L)
        scanner.stopScan(callback)

        failureCode?.let { code ->
            return@withLock errorResult("BLE scan failed: ${scanFailureMessage(code)}")
        }

        val devices = JSONArray()
        for (result in found.values) {
            val entry = JSONObject()
            entry.put("address", result.device.address)
            entry.put("name", result.device.name ?: result.scanRecord?.deviceName ?: "(unnamed)")
            entry.put("rssi", result.rssi)
            devices.put(entry)
        }

        val out = JSONObject()
        out.put("status", "ok")
        out.put("scan_seconds", seconds)
        out.put("device_count", devices.length())
        out.put("devices", devices)
        out
    }

    private fun errorResult(message: String): JSONObject {
        val out = JSONObject()
        out.put("status", "error")
        out.put("message", message)
        return out
    }
}

/** Human-readable text for ScanCallback.SCAN_FAILED_* codes -- the
 * platform only gives you an int. */
fun scanFailureMessage(code: Int): String = when (code) {
    ScanCallback.SCAN_FAILED_ALREADY_STARTED ->
        "a scan was already running (SCAN_FAILED_ALREADY_STARTED) -- two scans overlapped despite BleScanLock; shouldn't happen, worth a bug report if seen"
    ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED ->
        "app registration failed (SCAN_FAILED_APPLICATION_REGISTRATION_FAILED) -- often Android's scan-throttling kicking in after too many start/stop cycles in a short window"
    ScanCallback.SCAN_FAILED_FEATURE_UNSUPPORTED -> "BLE scanning unsupported on this radio (SCAN_FAILED_FEATURE_UNSUPPORTED)"
    ScanCallback.SCAN_FAILED_INTERNAL_ERROR -> "internal Bluetooth stack error (SCAN_FAILED_INTERNAL_ERROR) -- toggling Bluetooth off/on often clears this"
    else -> "unknown error code $code"
}
