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
import android.bluetooth.BluetoothStatusCodes
import android.os.Build
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
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
    private var hubChar: BluetoothGattCharacteristic? = null

    // Single-outstanding-request bookkeeping for sendAndAwait(). Fine to
    // have only one slot: every LWP command this class currently sends
    // (describePort's mode-by-mode walk, setLedRgb) is awaited to
    // completion before the next one is issued -- see each function's own
    // sequential suspend calls. Guarded by pendingLock because writes are
    // completed from onCharacteristicChanged, which runs on the BLE
    // callback thread, not the caller's coroutine.
    private val pendingLock = Any()
    private var pendingExpectedTypes: Set<Int> = emptySet()
    private var pendingDeferred: CompletableDeferred<ByteArray>? = null

    // Live APOS (mode 3, magnet-based true absolute position) cache, one
    // entry per subscribed port -- updated continuously from unsolicited
    // Port Value (Single) (0x45) pushes in onCharacteristicChanged, not
    // just request/response like sendAndAwait's callers. A port only
    // appears here once ensureAposSubscription() has subscribed it; see
    // that function for why a fresh subscribe-and-wait is needed before
    // the first read.
    private val aposState = java.util.concurrent.ConcurrentHashMap<Int, Int>()
    private val aposSubscribedPorts = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()

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
                val type = Lwp.messageType(bytes)

                // Port Value (Single) pushes are continuous, EXPECTED
                // background traffic once a port is APOS-subscribed (see
                // ensureAposSubscription) -- a servo sweeping ~90 degrees
                // in ~1.5s crosses whole-degree boundaries many times a
                // second, each one a separate 0x45 push at deltaInterval=1.
                // Hex-dumping every single one to CommandBus (meant for a
                // human skimming the phone's on-screen log) turned that
                // log into an unreadable flood the moment gotoApos's live
                // subscription started running. Still fully processed
                // below (aposState keeps updating) -- just not logged.
                if (type != Lwp.MSG_PORT_VALUE_SINGLE) {
                    CommandBus.post("[$label] <- ${bytes.joinToString(" ") { "%02x".format(it) }}")
                }
                // Hand off to whichever sendAndAwait() call is currently
                // waiting, if this reply's Message Type is one it asked
                // for. Deliberately NOT unconditional (any reply completes
                // it) -- e.g. describe_port's per-mode queries and a
                // concurrent unsolicited message (Hub Attached I/O,
                // Generic Error) could otherwise complete the wrong wait
                // with the wrong bytes.
                synchronized(pendingLock) {
                    val deferred = pendingDeferred
                    if (deferred != null && !deferred.isCompleted && type != null && type in pendingExpectedTypes) {
                        deferred.complete(bytes)
                    }
                }
                // Unsolicited (not tied to any single sendAndAwait call) --
                // a subscribed port pushes these continuously as its value
                // changes, independent of whichever request happens to be
                // in flight right now.
                if (type == Lwp.MSG_PORT_VALUE_SINGLE) {
                    val portId = Lwp.messagePortId(bytes)
                    if (portId != null && portId in aposSubscribedPorts) {
                        Lwp.parseApos(bytes)?.let { aposState[portId] = it }
                    }
                }
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
        this.hubChar = hubChar

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
        hubChar = null
        aposState.clear()
        aposSubscribedPorts.clear()
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
        hubChar = null
        aposState.clear()
        aposSubscribedPorts.clear()
        state = HubConnState.ERROR
        lastError = message
        CommandBus.post("[$label] x $message")
        return statusJson()
    }

    // -- LWP commands ----------------------------------------------------
    //
    // Everything below is the "stage after this one" the class doc
    // mentions: real Port Output/Information Request messages, not just
    // proving the notify subscription works.
    //   - describePort: read-only diagnostic (Port Information + Port
    //     Mode Information requests), safe to call any time.
    //   - setLedColor: the first real *output* command, and the reliable
    //     one -- LEGO's fixed Mode-0 color palette, the same path
    //     pylgbst/node-poweredup actually use and the one confirmed to
    //     visibly change our own hub's LED.
    //   - setLedRgb: Mode-1 direct RGB, spec-legal but NOT confirmed to
    //     render on our hub -- see its own doc comment.

    @SuppressLint("MissingPermission") // caller (ProbeService) verifies permissions first
    private fun writeToHub(bytes: ByteArray): Boolean {
        val g = gatt ?: return false
        val char = hubChar ?: return false
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            g.writeCharacteristic(char, bytes, BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE) ==
                BluetoothStatusCodes.SUCCESS
        } else {
            @Suppress("DEPRECATION") // pre-API33 write path -- minSdk 26 needs this
            char.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
            @Suppress("DEPRECATION")
            char.value = bytes
            @Suppress("DEPRECATION")
            g.writeCharacteristic(char)
        }
    }

    /** Writes an LWP message and waits for the next notification whose
     * Message Type is one of expectedTypes. Returns null on write
     * failure or timeout -- callers turn that into a proper error JSON
     * rather than throwing, same style as connect()'s own fail() calls. */
    private suspend fun sendAndAwait(message: ByteArray, expectedTypes: Set<Int>, timeoutMs: Long): ByteArray? {
        val deferred = CompletableDeferred<ByteArray>()
        synchronized(pendingLock) {
            pendingExpectedTypes = expectedTypes
            pendingDeferred = deferred
        }
        if (!writeToHub(message)) {
            synchronized(pendingLock) { if (pendingDeferred === deferred) pendingDeferred = null }
            return null
        }
        val result = withTimeoutOrNull(timeoutMs) { deferred.await() }
        synchronized(pendingLock) { if (pendingDeferred === deferred) pendingDeferred = null }
        return result
    }

    /** Port A-D diagnostic -- queries the LIVE device for its actual mode
     * table (name + raw range per mode), same purpose as
     * hub_controller.py's describe_port: ground truth about what a mode
     * actually looks like on the wire, instead of assuming. Two BLE
     * round-trips per mode (NAME, then RAW) plus one to enumerate modes
     * at all, so this is meant for occasional diagnostic use, not a hot
     * path -- same tradeoff the old describe_port's docstring called out
     * against pylgbst's brute-force describe_possible_modes(). */
    suspend fun describePort(port: String): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        val portId = Lwp.PORTS[port.uppercase()]
            ?: return out.put("status", "error")
                .put("message", "unknown port '$port', expected one of ${Lwp.PORTS.keys.sorted()}")

        val infoBytes = sendAndAwait(Lwp.portInformationRequest(portId), setOf(Lwp.MSG_PORT_INFORMATION), 3000)
            ?: return out.put("status", "error")
                .put("message", "no reply to Port Information Request on port $port -- nothing attached there?")
        val info = Lwp.parsePortInformationModeInfo(infoBytes)
            ?: return out.put("status", "error").put("message", "malformed Port Information reply")

        val modesOut = JSONArray()
        val presentModes = info.inputModes or info.outputModes
        for (mode in 0 until info.totalModeCount) {
            if ((presentModes shr mode) and 1 == 0) continue // mode number reserved/unused on this device

            val nameBytes = sendAndAwait(
                Lwp.portModeInformationRequest(portId, mode, Lwp.MODE_INFO_NAME),
                setOf(Lwp.MSG_PORT_MODE_INFORMATION), 2000,
            )
            val rawBytes = sendAndAwait(
                Lwp.portModeInformationRequest(portId, mode, Lwp.MODE_INFO_RAW),
                setOf(Lwp.MSG_PORT_MODE_INFORMATION), 2000,
            )

            val modeOut = JSONObject()
            modeOut.put("mode", mode)
            modeOut.put("name", nameBytes?.let { Lwp.parseModeName(it) } ?: JSONObject.NULL)
            Lwp.parseModeRawRange(rawBytes ?: ByteArray(0))?.let { (min, max) ->
                modeOut.put("raw_min", min).put("raw_max", max)
            }
            modeOut.put("input", (info.inputModes shr mode) and 1 == 1)
            modeOut.put("output", (info.outputModes shr mode) and 1 == 1)
            modesOut.put(modeOut)
        }

        CommandBus.post("[$label] describe_port $port -> ${modesOut.length()} mode(s) of ${info.totalModeCount} total")
        return out.put("status", "ok")
            .put("port", port.uppercase())
            .put("port_id", portId)
            .put("total_mode_count", info.totalModeCount)
            .put("modes", modesOut)
    }

    /** Sets the hub's own status LED to one of LEGO's fixed Mode-0
     * colors (see Lwp.LedColor) -- the RELIABLE path. Unlike setLedRgb
     * below, this is the exact command pylgbst's README example and
     * node-poweredup both use, and the one confirmed to actually change
     * the physical LED on our own hub. Prefer this over setLedRgb unless
     * you specifically need a color outside LEGO's 11-entry palette and
     * are prepared for it possibly not rendering (see setLedRgb's doc). */
    suspend fun setLedColor(colorIndex: Int): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        if (colorIndex !in 0..10 && colorIndex != 255) {
            return out.put("status", "error").put("message", "color index must be 0-10 or 255 (NONE)")
        }
        if (!writeToHub(Lwp.setHubLedColor(colorIndex))) {
            return out.put("status", "error").put("message", "BLE write failed")
        }
        CommandBus.post("[$label] LED -> color index $colorIndex")
        return out.put("status", "ok").put("port", "LED").put("color_index", colorIndex)
    }

    /** Sets the hub's own status LED to an exact R/G/B color via Mode 1
     * (direct RGB) -- the first real Port Output Command this class
     * sent, and still spec-legal, but NOT reliable: confirmed against
     * our own hub that this write is accepted (BLE write succeeds, same
     * write path describe_port's 0x21/0x22 requests use successfully)
     * without the physical LED visibly changing. The spec's own
     * WriteDirectModeData section hints why -- a mode-1 write can be
     * stored without being shown unless the port's currently active mode
     * is actually mode 1, which a bare WriteDirectModeData does not
     * change. Prefer setLedColor() above for anything that needs to
     * actually be seen; this is kept for a custom color outside LEGO's
     * 11-entry palette, and as the basis for trying a proper mode switch
     * (Port Input Format Setup, 0x41) first if that's ever worth adding. */
    suspend fun setLedRgb(r: Int, g: Int, b: Int): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        if (!writeToHub(Lwp.setHubLedRgb(r, g, b))) {
            return out.put("status", "error").put("message", "BLE write failed")
        }
        CommandBus.post("[$label] LED -> rgb($r, $g, $b) [mode 1 -- unconfirmed, may not render]")
        return out.put("status", "ok").put("port", "LED").put("r", r).put("g", g).put("b", b)
    }

    /** Continuous regulated speed on one motor port, -1.0..1.0, runs
     * until the next stop/command -- same semantics and parameter shape
     * as hub_controller.py's set_speed(port, speed, invert), just sent
     * as StartSpeed (0x07) directly instead of through pylgbst.
     *
     * invert: some motors are mounted with their physical rotation
     * direction reversed relative to the software's positive-speed
     * convention (see the front/rear hub wheel wiring). Rather than
     * making every caller remember to negate `speed` for those specific
     * ports, invert=True does it here, once -- same reasoning as the old
     * Python version's own docstring. */
    suspend fun setSpeed(port: String, speed: Double, invert: Boolean = false): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        val portId = Lwp.PORTS[port.uppercase()]
            ?: return out.put("status", "error")
                .put("message", "unknown port '$port', expected one of ${Lwp.PORTS.keys.sorted()}")

        val actualSpeed = if (invert) -speed else speed
        val speedByte = Lwp.speedToByte(actualSpeed)
        if (!writeToHub(Lwp.startSpeed(portId, speedByte))) {
            return out.put("status", "error").put("message", "BLE write failed")
        }
        CommandBus.post("[$label] port $port -> speed $speed${if (invert) " (inverted)" else ""}")
        return out.put("status", "ok").put("port", port.uppercase()).put("speed", speed).put("invert", invert)
    }

    /** Timed regulated drive on one motor port -- runs at `speed` for
     * `durationS` seconds then auto-brakes, entirely handled by the
     * hub's own firmware timer (StartSpeedForTime, sub-command 0x09)
     * rather than this app sleeping and sending a follow-up stop. That
     * matters for multi-wheel commands (DriveController's go_forward
     * etc): each wheel's hub times its own motor independently, so all
     * four stay in sync even though the handful of BLE writes that kick
     * them off aren't perfectly simultaneous -- app-side sleep+stop
     * would add that skew back in, once per wheel, on top of BLE write
     * latency each way. Same invert convention as setSpeed. */
    suspend fun driveForTime(
        port: String, speed: Double, durationS: Double, invert: Boolean = false, maxPower: Double = 1.0,
    ): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        val portId = Lwp.PORTS[port.uppercase()]
            ?: return out.put("status", "error")
                .put("message", "unknown port '$port', expected one of ${Lwp.PORTS.keys.sorted()}")

        val actualSpeed = if (invert) -speed else speed
        val speedByte = Lwp.speedToByte(actualSpeed)
        val timeMs = Math.round(durationS.coerceIn(0.0, 65.0) * 1000).toInt()
        val maxPowerPct = Math.round(maxPower.coerceIn(0.0, 1.0) * 100).toInt()
        if (!writeToHub(Lwp.startSpeedForTime(portId, timeMs, speedByte, maxPowerPct))) {
            return out.put("status", "error").put("message", "BLE write failed")
        }
        CommandBus.post("[$label] port $port -> speed $speed for ${durationS}s${if (invert) " (inverted)" else ""}")
        return out.put("status", "ok").put("port", port.uppercase())
            .put("speed", speed).put("duration_s", durationS).put("invert", invert)
    }

    /** Stops one motor port -- StartSpeedForTime(0ms, endState=BRAKE),
     * exactly matching pylgbst's Motor.stop() (`self.timed(0)`), not
     * StartSpeed(0). See Lwp.stopMotor's doc for why that distinction
     * matters: this is the specific command already proven to stop
     * these motors on this hardware, not a simplification of it. */
    suspend fun stop(port: String): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        val portId = Lwp.PORTS[port.uppercase()]
            ?: return out.put("status", "error")
                .put("message", "unknown port '$port', expected one of ${Lwp.PORTS.keys.sorted()}")

        if (!writeToHub(Lwp.stopMotor(portId))) {
            return out.put("status", "error").put("message", "BLE write failed")
        }
        CommandBus.post("[$label] port $port -> stop (brake)")
        return out.put("status", "ok").put("port", port.uppercase())
    }

    /** Subscribes a port to live APOS (mode 3) updates if it isn't
     * already, then waits for at least one value to have actually
     * arrived -- the 0x47 reply to the subscribe request only confirms
     * the format was applied, not that the hub has pushed a value yet,
     * same distinction hub_controller.py's _ensure_angle_subscription
     * drew for mode 2. Returns null on subscribe failure or timeout;
     * returns the CACHED value immediately (no wait) if this port was
     * already subscribed, since aposState keeps updating live in the
     * background via onCharacteristicChanged. */
    private suspend fun ensureAposSubscription(portId: Int, waitTimeoutMs: Long = 3000): Int? {
        if (portId !in aposSubscribedPorts) {
            val subscribed = sendAndAwait(
                Lwp.portInputFormatSetupSingle(portId, Lwp.MOTOR_MODE_APOS),
                setOf(Lwp.MSG_PORT_INPUT_FORMAT_SINGLE), waitTimeoutMs,
            )
            if (subscribed == null) return null
            aposSubscribedPorts.add(portId)
        }
        if (aposState[portId] == null) {
            val deadline = System.currentTimeMillis() + waitTimeoutMs
            while (aposState[portId] == null && System.currentTimeMillis() < deadline) {
                delay(20)
            }
        }
        return aposState[portId]
    }

    /** Reads a motor's TRUE magnet-based absolute position (APOS) --
     * needs no calibration to be meaningful (unlike mode 2/POS, a
     * relative counter that resets wherever tracking starts each
     * session), but DOES need presetZero() called once, with the motor
     * held at the position that should read 0, before its numbers mean
     * anything to a caller (e.g. "0 = looking forward" for the head-tilt
     * servo). */
    suspend fun readApos(port: String): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        val portId = Lwp.PORTS[port.uppercase()]
            ?: return out.put("status", "error")
                .put("message", "unknown port '$port', expected one of ${Lwp.PORTS.keys.sorted()}")

        val apos = ensureAposSubscription(portId)
            ?: return out.put("status", "error").put("message", "no APOS data from encoder on port $port")
        return out.put("status", "ok").put("port", port.uppercase()).put("apos", apos)
    }

    /** Recalibrates this motor's TRUE zero reference so its CURRENT
     * physical position becomes APOS 0 going forward -- a genuine
     * firmware-level recalibration (see Lwp.presetPosition's doc), not
     * just resetting a software counter. IMPORTANT: call this only while
     * the motor is actually at the physical position that should become
     * 0 (e.g. the head-tilt servo held level, looking straight forward)
     * -- calling it anywhere else bakes in a wrong reference just as
     * surely as the one it's meant to fix. */
    suspend fun presetZero(port: String): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        val portId = Lwp.PORTS[port.uppercase()]
            ?: return out.put("status", "error")
                .put("message", "unknown port '$port', expected one of ${Lwp.PORTS.keys.sorted()}")

        if (!writeToHub(Lwp.presetPosition(portId, 0))) {
            return out.put("status", "error").put("message", "BLE write failed")
        }
        // Drop any cached/subscribed state and re-subscribe fresh -- we
        // want the NEXT read to reflect the just-recalibrated zero, not a
        // value cached from before the preset took effect.
        aposSubscribedPorts.remove(portId)
        aposState.remove(portId)
        delay(150) // brief settle so the firmware applies the recalibration before we re-read
        val after = ensureAposSubscription(portId)

        CommandBus.post("[$label] port $port -> preset zero (apos after: ${after ?: "unknown"})")
        val result = out.put("status", "ok").put("port", port.uppercase())
        if (after != null) result.put("apos_after_preset", after) else result.put("apos_after_preset", JSONObject.NULL)
        return result
    }

    /** Moves a motor to an absolute APOS target angle (degrees), using
     * the current APOS reading to compute the shortest relative delta
     * (wrapping across the -180/180 boundary) and sending that as a
     * StartSpeedForDegrees move -- same overall approach as
     * hub_controller.py's home_angle, adapted from pylgbst's blocking
     * angled() to this class's fire-and-forget style: rather than
     * waiting for Port Output Command Feedback (0x82, not parsed yet --
     * see Lwp's own note on that), this sends the move, waits a fixed
     * settle window, then reports whatever APOS actually reads
     * afterward -- the REAL resulting position, not an assumed one,
     * regardless of whether the settle timing was exactly right.
     *
     * targetDegrees should stay roughly within -170..170 -- APOS itself
     * is hard-bounded to -180..179 (confirmed via describe_port), so
     * there's no "multiple turns" concept the way a POS-based approach
     * might assume. */
    suspend fun gotoApos(
        port: String, targetDegrees: Double,
        speed: Double = 0.4, maxPower: Double = 0.5, invert: Boolean = false, settleMs: Long = 1500,
    ): JSONObject {
        val out = JSONObject()
        if (state != HubConnState.CONNECTED) {
            return out.put("status", "error").put("message", "hub not connected")
        }
        val portId = Lwp.PORTS[port.uppercase()]
            ?: return out.put("status", "error")
                .put("message", "unknown port '$port', expected one of ${Lwp.PORTS.keys.sorted()}")

        val current = ensureAposSubscription(portId)
            ?: return out.put("status", "error").put("message", "no APOS data from encoder on port $port")

        var delta = targetDegrees - current
        if (delta > 180) delta -= 360
        if (delta < -180) delta += 360
        val appliedDelta = if (invert) -delta else delta

        // StartSpeedForDegrees wants an UNSIGNED degrees count -- a
        // negative delta is sent as a positive degrees with the speed's
        // sign flipped instead, exactly matching pylgbst's angled().
        var degrees = Math.round(appliedDelta).toInt()
        var signedSpeed = speed
        if (degrees < 0) {
            degrees = -degrees
            signedSpeed = -signedSpeed
        }
        val speedByte = Lwp.speedToByte(signedSpeed)
        val maxPowerPct = Math.round(maxPower.coerceIn(0.0, 1.0) * 100).toInt()

        if (!writeToHub(Lwp.startSpeedForDegrees(portId, degrees, speedByte, maxPowerPct))) {
            return out.put("status", "error").put("message", "BLE write failed")
        }
        delay(settleMs)
        val finalApos = aposState[portId] ?: current

        CommandBus.post("[$label] port $port -> goto_apos target=$targetDegrees final=$finalApos")
        return out.put("status", "ok")
            .put("port", port.uppercase())
            .put("target", targetDegrees)
            .put("final_apos", finalApos)
            .put("error", targetDegrees - finalApos)
    }
}
