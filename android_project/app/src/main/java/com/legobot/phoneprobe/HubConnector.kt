package com.legobot.phoneprobe

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject
import java.util.UUID

// Standard LEGO Wireless Protocol (LWP) BLE UUIDs -- LEGO's own published
// constants (lego.github.io/lego-ble-wireless-protocol-docs), the same
// ones pylgbst/pybricks/node-poweredup all use under the hood.
// hub_controller.py never has to name these itself because pylgbst's
// get_connection_bleak() hides them -- Android has no pylgbst
// equivalent, so this is the one place on the phone that has to know
// them explicitly. If service discovery ever comes back empty against
// a real hub, this is the first thing to double-check against LEGO's
// spec (firmware UUID changes would show up as exactly that failure).
private val LEGO_HUB_SERVICE_UUID = UUID.fromString("00001623-1212-efde-1623-785feabcd123")
private val LEGO_HUB_CHARACTERISTIC_UUID = UUID.fromString("00001624-1212-efde-1623-785feabcd123")
private val CLIENT_CHARACTERISTIC_CONFIG_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

enum class HubConnState { DISCONNECTED, SCANNING, CONNECTING, SUBSCRIBING, CONNECTED, ERROR }

/**
 * Android's answer to hub_controller.py's connect() -- opens a real GATT
 * link to one named hub and subscribes to LWP's single read/write/notify
 * characteristic. Deliberately stops there: this proves the phone can
 * hold a live two-way BLE link (any bytes the hub sends back get logged
 * to CommandBus as hex, proving the notify subscription actually works),
 * but does NOT send any LWP command messages or parse attached-IO/port
 * info -- that's motor control, the stage after this one.
 *
 * One instance per hub (front/rear) rather than shared -- LEGO hubs only
 * accept ONE central connection at a time (same constraint
 * hub_controller.py's module docstring notes as the reason the Pi
 * connects its two hubs sequentially, not in parallel), and front/rear
 * are independent BLE links with independent state regardless.
 */
class HubConnector(private val context: Context, val hubName: String, val label: String) {

    @Volatile var state: HubConnState = HubConnState.DISCONNECTED
        private set
    @Volatile var lastError: String? = null
        private set
    @Volatile var lastAddress: String? = null
        private set

    private var gatt: BluetoothGatt? = null

    @SuppressLint("MissingPermission") // caller (ProbeService) verifies permissions first
    suspend fun connect(scanTimeoutSeconds: Int = 12): JSONObject {
        disconnect() // clean slate -- don't leak a previous attempt's GATT client

        val btManager = context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        val adapter = btManager.adapter
            ?: return fail("No Bluetooth adapter on this device.")
        if (!adapter.isEnabled) {
            return fail("Bluetooth is off -- enable it in the phone's quick settings.")
        }
        val scanner = adapter.bluetoothLeScanner
            ?: return fail("BluetoothLeScanner unavailable (adapter busy or off).")

        // --- 1. scan for a device advertising exactly this hub's name ---
        state = HubConnState.SCANNING
        CommandBus.post("[$label] scanning for \"$hubName\"…")
        val found = CompletableDeferred<BluetoothDevice?>()
        var scanFailureCode: Int? = null
        val scanCallback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                if (result.device.name == hubName && !found.isCompleted) {
                    found.complete(result.device)
                }
            }
            override fun onScanFailed(errorCode: Int) {
                // Previously unhandled entirely -- a rejected/throttled scan
                // (e.g. two scans racing on the one radio -- see
                // BleScanLock) used to look identical to "scanned the full
                // window, genuinely never saw it," which sent debugging in
                // the wrong direction (hub/firmware) instead of the right
                // one (scan contention).
                scanFailureCode = errorCode
                if (!found.isCompleted) found.complete(null)
            }
        }
        // LOW_LATENCY (continuous listening) instead of the default
        // LOW_POWER duty-cycled mode -- this is a short, one-shot,
        // time-critical scan for one specific advertiser, not a broad
        // background sweep, so the higher radio usage for a few seconds
        // is the right tradeoff. LOW_POWER's long listen-gaps are enough
        // to plausibly miss a single device's advertisement within an
        // 8-12s window even with zero contention.
        val scanSettings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()

        val device = BleScanLock.withLock {
            scanner.startScan(null, scanSettings, scanCallback)
            val result = withTimeoutOrNull(scanTimeoutSeconds * 1000L) { found.await() }
            scanner.stopScan(scanCallback)
            result
        }

        scanFailureCode?.let { code ->
            return fail("BLE scan failed: ${scanFailureMessage(code)}")
        }
        if (device == null) {
            return fail("never saw a hub named \"$hubName\" after ${scanTimeoutSeconds}s -- is it on and unpaired from anything else?")
        }
        lastAddress = device.address
        CommandBus.post("[$label] found $hubName at ${device.address}, connecting…")

        // --- 2. open the GATT connection ---
        state = HubConnState.CONNECTING
        val connected = CompletableDeferred<Boolean>()
        val servicesDone = CompletableDeferred<Boolean>()
        val subscribed = CompletableDeferred<Boolean>()

        val callback = object : BluetoothGattCallback() {
            override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    if (!connected.isCompleted) connected.complete(true)
                    g.discoverServices()
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    val wasConnected = state == HubConnState.CONNECTED
                    state = HubConnState.DISCONNECTED
                    if (!connected.isCompleted) connected.complete(false)
                    if (wasConnected) CommandBus.post("[$label] hub disconnected")
                }
            }

            override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
                if (!servicesDone.isCompleted) servicesDone.complete(status == BluetoothGatt.GATT_SUCCESS)
            }

            @Suppress("DEPRECATION") // pre-API33 callback signature -- minSdk 26 needs this overload
            override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
                val bytes = characteristic.value ?: return
                CommandBus.post("[$label] <- ${bytes.joinToString(" ") { "%02x".format(it) }}")
            }

            override fun onDescriptorWrite(g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
                if (!subscribed.isCompleted) subscribed.complete(status == BluetoothGatt.GATT_SUCCESS)
            }
        }

        gatt = device.connectGatt(context, false, callback)

        val gattConnected = withTimeoutOrNull(10_000L) { connected.await() } ?: false
        if (!gattConnected) {
            return fail("GATT connect failed or timed out")
        }

        val discovered = withTimeoutOrNull(10_000L) { servicesDone.await() } ?: false
        if (!discovered) {
            return fail("service discovery failed or timed out")
        }

        val hubService = gatt?.getService(LEGO_HUB_SERVICE_UUID)
            ?: return fail("hub didn't advertise the LEGO Wireless Protocol service ($LEGO_HUB_SERVICE_UUID) -- wrong device, or check the UUID against LEGO's spec")
        val hubChar = hubService.getCharacteristic(LEGO_HUB_CHARACTERISTIC_UUID)
            ?: return fail("LWP service found but missing its characteristic $LEGO_HUB_CHARACTERISTIC_UUID")

        // --- 3. subscribe to notifications -- the actual proof of a live
        // two-way link, not just an open GATT connection ---
        state = HubConnState.SUBSCRIBING
        gatt?.setCharacteristicNotification(hubChar, true)
        val cccd = hubChar.getDescriptor(CLIENT_CHARACTERISTIC_CONFIG_UUID)
            ?: return fail("characteristic has no CCCD descriptor -- can't enable notifications")
        @Suppress("DEPRECATION") // pre-API33 descriptor.setValue -- minSdk 26 needs this
        cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        @Suppress("DEPRECATION")
        gatt?.writeDescriptor(cccd)

        val ok = withTimeoutOrNull(5_000L) { subscribed.await() } ?: false
        if (!ok) {
            return fail("failed to subscribe to hub notifications")
        }

        state = HubConnState.CONNECTED
        lastError = null
        CommandBus.post("[$label] connected + subscribed -- link is live")
        return statusJson()
    }

    @SuppressLint("MissingPermission")
    fun disconnect() {
        val wasConnected = state == HubConnState.CONNECTED
        gatt?.disconnect()
        gatt?.close()
        gatt = null
        if (wasConnected) CommandBus.post("[$label] disconnected")
        state = HubConnState.DISCONNECTED
    }

    fun statusJson(): JSONObject {
        val out = JSONObject()
        out.put("hub", label)
        out.put("name", hubName)
        out.put("state", state.name.lowercase())
        out.put("address", lastAddress)
        out.put("error", lastError)
        return out
    }

    @SuppressLint("MissingPermission") // caller (ProbeService) verifies permissions first
    private fun fail(message: String): JSONObject {
        gatt?.disconnect()
        gatt?.close()
        gatt = null
        state = HubConnState.ERROR
        lastError = message
        CommandBus.post("[$label] x $message")
        return statusJson()
    }
}
