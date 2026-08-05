package com.legobot.phoneprobe

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Raw LEGO Wireless Protocol (LWP 3.0) message encoding/decoding.
 *
 * hub_controller.py never had to know any of this -- pylgbst hid the wire
 * format behind Python objects (`motor._describe_mode()`, `hub.led`, etc).
 * Android has no pylgbst equivalent, so HubConnector talks LWP directly,
 * and this is the one place that knows the actual byte layouts, straight
 * from LEGO's published spec:
 * https://lego.github.io/lego-ble-wireless-protocol-docs/
 *
 * Every LWP message on the single 1624 characteristic shares a 3-byte
 * Common Header: (Length, HubID=0x00, MessageType), followed by a
 * message-type-specific payload. Length includes itself, and covers the
 * whole message. All of *our* messages are well under 127 bytes, so the
 * escaped 2-byte length encoding (bit 7 set) never applies here --
 * encodeMessage() asserts that rather than silently mis-encoding it.
 */
object Lwp {

    // -- Message Types (LEGO Hub Characteristic 0x1624) --------------------
    const val MSG_PORT_INFO_REQUEST = 0x21          // Down -- "tell me about this port"
    const val MSG_PORT_MODE_INFO_REQUEST = 0x22      // Down -- "tell me about this port's mode"
    const val MSG_PORT_INFORMATION = 0x43            // Up   -- reply to 0x21
    const val MSG_PORT_MODE_INFORMATION = 0x44       // Up   -- reply to 0x22
    const val MSG_PORT_OUTPUT_COMMAND = 0x81         // Down -- drive a motor/light/etc
    const val MSG_PORT_OUTPUT_COMMAND_FEEDBACK = 0x82 // Up  -- reply to 0x81 (not consumed yet)

    // -- Port Information Request (0x21) "Information Type" ----------------
    // 0x00 = live Port Value, 0x02 = possible mode combinations -- only
    // 0x01 (Mode Info: capabilities + mode count + mode bitmasks) is used
    // here, since that's all describe_port needs to then walk each mode.
    const val PORT_INFO_MODE_INFO = 0x01

    // -- Port Mode Information Request (0x22) "Information Type" -----------
    // Full list per spec is NAME/RAW/PCT/SI/SYMBOL/MAPPING/MOTOR_BIAS/
    // CAPABILITY_BITS/VALUE_FORMAT -- only pulling NAME + RAW here for this
    // first pass (parity with what a human actually reads off
    // hub_controller.py's old describe_port output: mode name + range).
    // Extending to PCT/SI/SYMBOL later is just one more sendAndAwait call
    // per mode in HubConnector.describePort, same shape as these two.
    const val MODE_INFO_NAME = 0x00
    const val MODE_INFO_RAW = 0x01

    // -- Port Output Command (0x81) sub-commands ----------------------------
    const val SUBCMD_WRITE_DIRECT_MODE_DATA = 0x51

    // -- Startup/Completion Information byte (Port Output Command) ---------
    // Low nibble = Completion (0x00 no action, 0x01 request feedback on
    // 0x82), high nibble = Startup (0x00 buffer, 0x10 execute immediately).
    // 0x11 = execute immediately + ask for feedback -- the standard
    // combination every LWP client library (pylgbst/pybricks/
    // node-poweredup) uses for one-shot direct commands like this.
    // Feedback (0x82) isn't parsed yet (see MSG_PORT_OUTPUT_COMMAND_FEEDBACK) --
    // today "success" means the BLE write itself went through.
    const val EXECUTE_IMMEDIATE_WITH_FEEDBACK = 0x11

    // Hub connector ports A-D -- identical mapping to old
    // hub_controller.py's PORTS dict, so port letters mean the same thing
    // on both sides of the Pi->phone migration.
    val PORTS: Map<String, Int> = mapOf("A" to 0x00, "B" to 0x01, "C" to 0x02, "D" to 0x03)

    // The hub's own built-in status LED. Per LEGO's Port ID range (0-49 =
    // physical connectors, 50-100 = internal devices), every Control+/
    // Technic hub has an "RGB Light" permanently attached at port 0x32
    // (50) -- confirmed against independent hub port-dump logs (e.g.
    // pybricks discovery output showing an RGB Light device at
    // "Port: 0x32 / 50" on hubs with no physical port by that name).
    // This is NOT one of the PORTS above -- it's not a connector a motor
    // could ever be plugged into.
    const val HUB_LED_PORT = 0x32

    // RGB Light's Mode 1 ("RGB O") -- direct R/G/B bytes, 0-255 each, per
    // LEGO's own WriteDirectModeData example in the spec text itself
    // ("...set RGB values to 00,33,00 (direct to mode 1)"). Mode 0 is a
    // different, LEGO-predefined 11-color index instead; not used here --
    // direct RGB is unambiguous and doesn't require guessing LEGO's index
    // table.
    const val LED_MODE_RGB = 0x01

    /** Builds one full LWP message: (Length, HubID=0, MessageType, payload...). */
    fun encodeMessage(messageType: Int, vararg payload: Int): ByteArray {
        val body = ByteArray(payload.size + 2)
        body[0] = 0x00 // Hub ID -- "NOT USE at the moment! Always set to 0x00" per spec
        body[1] = messageType.toByte()
        for (i in payload.indices) body[i + 2] = payload[i].toByte()
        val length = body.size + 1 // +1 for the length byte itself
        require(length in 1..127) {
            "message length $length needs the escaped 2-byte length encoding -- " +
                "not implemented, and none of our messages should ever be this long"
        }
        return byteArrayOf(length.toByte()) + body
    }

    fun portInformationRequest(portId: Int): ByteArray =
        encodeMessage(MSG_PORT_INFO_REQUEST, portId, PORT_INFO_MODE_INFO)

    fun portModeInformationRequest(portId: Int, mode: Int, infoType: Int): ByteArray =
        encodeMessage(MSG_PORT_MODE_INFO_REQUEST, portId, mode, infoType)

    fun writeDirectModeData(portId: Int, mode: Int, vararg data: Int): ByteArray =
        encodeMessage(
            MSG_PORT_OUTPUT_COMMAND, portId, EXECUTE_IMMEDIATE_WITH_FEEDBACK,
            SUBCMD_WRITE_DIRECT_MODE_DATA, mode, *data
        )

    fun setHubLedRgb(r: Int, g: Int, b: Int): ByteArray =
        writeDirectModeData(HUB_LED_PORT, LED_MODE_RGB, r.coerceIn(0, 255), g.coerceIn(0, 255), b.coerceIn(0, 255))

    /** Common Header's Message Type byte (offset 2), or null if too short to have one. */
    fun messageType(bytes: ByteArray): Int? = if (bytes.size >= 3) bytes[2].toInt() and 0xFF else null

    data class PortModeInfo(
        val capabilities: Int,
        val totalModeCount: Int,
        val inputModes: Int,   // bitmask, bit N set = mode N usable as input
        val outputModes: Int,  // bitmask, bit N set = mode N usable as output
    )

    /** Parses a Port Information (0x43) reply to a Mode Info (0x01) request.
     * Layout: (len, hub=0, 0x43, portId, infoType=0x01, capabilities,
     *          totalModeCount, inputModes:u16 LE, outputModes:u16 LE) -- 11 bytes total,
     * matching the spec's "Information Type 0x01 : 11 Bytes". */
    fun parsePortInformationModeInfo(bytes: ByteArray): PortModeInfo? {
        if (bytes.size < 11) return null
        val capabilities = bytes[5].toInt() and 0xFF
        val totalModeCount = bytes[6].toInt() and 0xFF
        val inputModes = (bytes[7].toInt() and 0xFF) or ((bytes[8].toInt() and 0xFF) shl 8)
        val outputModes = (bytes[9].toInt() and 0xFF) or ((bytes[10].toInt() and 0xFF) shl 8)
        return PortModeInfo(capabilities, totalModeCount, inputModes, outputModes)
    }

    /** Parses a Port Mode Information (0x44) reply to a NAME (0x00) request.
     * Layout: (len, hub=0, 0x44, portId, mode, infoType=0x00, name: NUL-padded ASCII, rest of message). */
    fun parseModeName(bytes: ByteArray): String {
        if (bytes.size <= 6) return ""
        val raw = bytes.copyOfRange(6, bytes.size)
        val nul = raw.indexOf(0)
        val trimmed = if (nul >= 0) raw.copyOfRange(0, nul) else raw
        return String(trimmed, Charsets.US_ASCII)
    }

    /** Parses a Port Mode Information (0x44) reply to a RAW (0x01) request.
     * Layout: same header as parseModeName, then (min: float32 LE, max: float32 LE) -- 14 bytes total,
     * matching the spec's "Information Type 0x01 : 14 Bytes". Returns null if too short to hold both floats. */
    fun parseModeRawRange(bytes: ByteArray): Pair<Float, Float>? {
        if (bytes.size < 14) return null
        val bb = ByteBuffer.wrap(bytes, 6, 8).order(ByteOrder.LITTLE_ENDIAN)
        val min = bb.float
        val max = bb.float
        return min to max
    }
}
