package com.legobot.phoneprobe.relay

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Matches lego_pi/media_relay.py's `_pack()` exactly:
 *     struct.pack(">Bd", msg_type, time.time()) + payload
 * i.e. 1 byte type, 8 byte big-endian double (unix seconds), then payload.
 * Keeping this identical is the whole point -- it's what lets the
 * existing lego_brain/media_client.py on the PC talk to this phone
 * without a single line of PC-side code changing.
 */
const val TYPE_VIDEO: Byte = 0x01
const val TYPE_AUDIO: Byte = 0x02

fun packFrame(type: Byte, payload: ByteArray): ByteArray {
    val header = ByteBuffer.allocate(9).order(ByteOrder.BIG_ENDIAN)
    header.put(type)
    header.putDouble(System.currentTimeMillis() / 1000.0)
    val out = ByteArray(9 + payload.size)
    System.arraycopy(header.array(), 0, out, 0, 9)
    System.arraycopy(payload, 0, out, 9, payload.size)
    return out
}
