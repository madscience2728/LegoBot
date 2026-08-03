package com.legobot.phoneprobe.relay

import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow

/**
 * The single point where camera/mic capture threads hand off frames,
 * and where the websocket route reads from. DROP_OLDEST on overflow is
 * the "non-blocking, realtime" requirement in concrete form: if nothing
 * is connected, or the network can't keep up, frames get dropped here
 * rather than ever applying backpressure to the capture threads. A
 * slow/blocked network write must never stall the camera or mic loop.
 */
object MediaHub {
    val frames = MutableSharedFlow<ByteArray>(
        replay = 0,
        extraBufferCapacity = 64,
        onBufferOverflow = BufferOverflow.DROP_OLDEST
    )

    fun emit(type: Byte, payload: ByteArray) {
        frames.tryEmit(packFrame(type, payload))
    }
}
