package com.legobot.phoneprobe.relay

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

private const val SAMPLE_RATE = 16000
private const val FRAME_MS = 30
// 16000 * 0.030 = 480 samples, x2 bytes/sample (PCM16) = 960 bytes --
// this exact size is a hard requirement of webrtcvad on the PC side
// (10/20/30ms only), matching AUDIO_FRAME_SAMPLES in media_relay.py.
private const val FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS / 1000
private const val FRAME_BYTES = FRAME_SAMPLES * 2

/**
 * Continuous equivalent of MicProbe.kt's fixed-duration recording.
 * Reads in a loop sized to exactly FRAME_BYTES per iteration so every
 * emitted chunk is already a valid webrtcvad frame -- no buffering/
 * re-chunking needed on the PC side beyond what media_client.py
 * already does for the Pi.
 */
class MicStream(@Suppress("UNUSED_PARAMETER") context: Context) {

    private var recorder: AudioRecord? = null
    private val running = AtomicBoolean(false)
    private var captureThread: Thread? = null

    @SuppressLint("MissingPermission") // caller verifies RECORD_AUDIO permission first
    fun start() {
        val channelConfig = AudioFormat.CHANNEL_IN_MONO
        val audioFormat = AudioFormat.ENCODING_PCM_16BIT
        val minBufSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, channelConfig, audioFormat)
        if (minBufSize <= 0) return

        val bufferSize = maxOf(minBufSize, FRAME_BYTES * 4)
        val rec = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            SAMPLE_RATE, channelConfig, audioFormat, bufferSize
        )
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return
        }
        recorder = rec
        running.set(true)
        rec.startRecording()

        captureThread = thread(name = "MicStreamThread") {
            val chunk = ByteArray(FRAME_BYTES)
            while (running.get()) {
                var filled = 0
                // AudioRecord.read can return short reads -- loop until
                // we have a full, valid FRAME_BYTES chunk before emitting,
                // so every frame handed to MediaHub is exactly one
                // webrtcvad-sized frame, never a partial one.
                while (filled < FRAME_BYTES && running.get()) {
                    val n = rec.read(chunk, filled, FRAME_BYTES - filled)
                    if (n <= 0) break
                    filled += n
                }
                if (filled == FRAME_BYTES) {
                    MediaHub.emit(TYPE_AUDIO, chunk.copyOf())
                }
            }
        }
    }

    fun stop() {
        running.set(false)
        try { recorder?.stop() } catch (_: Exception) {}
        try { recorder?.release() } catch (_: Exception) {}
        recorder = null
        captureThread?.join(1000)
        captureThread = null
    }
}
