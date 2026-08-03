package com.legobot.phoneprobe

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream

/**
 * Proof-of-life for mic access, standing in for media_relay.py's
 * sounddevice/PortAudio path (which needs ALSA -- also not a thing on
 * Android). Records raw PCM16 mono at 16000 Hz -- deliberately the
 * same rate media_relay.py already uses -- for a fixed duration and
 * writes it straight to a file. No streaming, no VAD, no framing yet;
 * same reasoning as CameraProbe -- prove the mic + permission path
 * before adding the harder realtime pieces on top.
 */
class MicProbe(private val context: Context) {

    @SuppressLint("MissingPermission") // caller (ProbeService) verifies RECORD_AUDIO permission first
    suspend fun recordFor(seconds: Int): JSONObject = withContext(Dispatchers.IO) {
        val sampleRate = 16000
        val channelConfig = AudioFormat.CHANNEL_IN_MONO
        val audioFormat = AudioFormat.ENCODING_PCM_16BIT

        val minBufSize = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat)
        if (minBufSize <= 0) {
            return@withContext errorResult("Device doesn't support 16kHz mono PCM16 -- unexpected, but would need a rate change here and in media_relay.py's webrtcvad framing.")
        }

        val recorder = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            sampleRate, channelConfig, audioFormat,
            minBufSize * 2
        )

        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            recorder.release()
            return@withContext errorResult("AudioRecord failed to initialize (mic busy or permission denied at the OS level).")
        }

        val outFile = File(context.filesDir, "probe_recording.pcm")
        var totalBytes = 0
        try {
            recorder.startRecording()
            val buffer = ByteArray(minBufSize)
            val endAtMs = System.currentTimeMillis() + seconds * 1000L
            FileOutputStream(outFile).use { fos ->
                while (System.currentTimeMillis() < endAtMs) {
                    val read = recorder.read(buffer, 0, buffer.size)
                    if (read > 0) {
                        fos.write(buffer, 0, read)
                        totalBytes += read
                    }
                }
            }
        } finally {
            recorder.stop()
            recorder.release()
        }

        val out = JSONObject()
        out.put("status", "ok")
        out.put("seconds", seconds)
        out.put("sample_rate", sampleRate)
        out.put("bytes", totalBytes)
        out.put("saved_to", outFile.absolutePath)
        out
    }

    private fun errorResult(message: String): JSONObject {
        val out = JSONObject()
        out.put("status", "error")
        out.put("message", message)
        return out
    }
}
