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
    suspend fun scan(seconds: Int): JSONObject {
        val btManager = context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        val adapter = btManager.adapter
            ?: return errorResult("No Bluetooth adapter on this device.")
        if (!adapter.isEnabled) {
            return errorResult("Bluetooth is off -- enable it in the phone's quick settings.")
        }
        val scanner = adapter.bluetoothLeScanner
            ?: return errorResult("BluetoothLeScanner unavailable (adapter busy or off).")

        val found = LinkedHashMap<String, ScanResult>() // address -> latest result, de-duped
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                found[result.device.address] = result
            }
            override fun onScanFailed(errorCode: Int) {
                found["__error__"] = null as ScanResult? ?: return
            }
        }

        scanner.startScan(callback)
        delay(seconds * 1000L)
        scanner.stopScan(callback)

        val devices = JSONArray()
        for (result in found.values) {
            if (result == null) continue
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
        return out
    }

    private fun errorResult(message: String): JSONObject {
        val out = JSONObject()
        out.put("status", "error")
        out.put("message", message)
        return out
    }
}
