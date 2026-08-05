package com.legobot.phoneprobe

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong

/**
 * Owns the phone's local TextToSpeech engine. Deliberately NOT a
 * FaceBus-shaped pub/sub -- FaceBus exists because ProbeService (no UI)
 * needs to push a value into MainActivity (has the emojiFace TextView).
 * Voice has no such cross-component problem: nothing on screen needs
 * updating, so ProbeService just owns this directly. That also means
 * speech keeps working even if the app is backgrounded or the screen is
 * off -- worth having for a robot nobody's necessarily looking at,
 * unlike the face, which is inherently tied to the screen being visible.
 *
 * Android's TextToSpeech engine loads asynchronously (onInit callback,
 * not immediate) -- callers hitting speak() before that completes get a
 * clear "engine not ready yet" error rather than a silently-dropped or
 * queued-forever request, same "fail honestly" style as everything else
 * here (HubConnector's "hub not connected" checks, etc).
 */
class VoiceController(context: Context) {

    @Volatile private var ready = false
    @Volatile private var initError: String? = null
    @Volatile private var languageOk = true
    @Volatile private var voiceName: String? = null

    private val nextUtteranceId = AtomicLong(0)
    // Tracks in-flight utterances so /voice/status can report what's
    // currently speaking without polling tts.isSpeaking() (which some
    // engines report unreliably right at start/end of an utterance).
    private val speaking = ConcurrentHashMap.newKeySet<String>()

    private val tts: TextToSpeech = TextToSpeech(context.applicationContext) { status ->
        if (status == TextToSpeech.SUCCESS) {
            ready = true
            configureLanguage()
            CommandBus.post("voice: TTS engine ready")
        } else {
            initError = "TextToSpeech engine failed to initialize (status=$status)"
            CommandBus.post("voice: x $initError")
        }
    }

    init {
        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {
                utteranceId?.let { speaking.add(it) }
            }
            override fun onDone(utteranceId: String?) {
                utteranceId?.let { speaking.remove(it) }
            }
            @Suppress("DEPRECATION") // pre-API21 callback signature -- minSdk 26 still needs the override to exist
            override fun onError(utteranceId: String?) {
                utteranceId?.let { speaking.remove(it) }
            }
        })
    }

    /** Speaks `text` and returns once the engine has ACCEPTED it (queued
     * to speak), not once speech has finished -- same fire-and-forget
     * philosophy as HubConnector's driveForTime (the engine times
     * itself; we don't block a caller on however many seconds a
     * sentence takes to say). interrupt=true (default) cuts off
     * whatever's currently being said, matching how FaceBus.setEmoji
     * always overwrites rather than queuing expressions. */
    fun speak(text: String, interrupt: Boolean = true): JSONObject {
        val out = JSONObject()
        if (!ready) {
            return out.put("status", "error")
                .put("message", initError ?: "TTS engine not ready yet -- try again in a moment")
        }
        if (text.isBlank()) {
            return out.put("status", "error").put("message", "text must not be blank")
        }

        val utteranceId = "u${nextUtteranceId.incrementAndGet()}"
        val queueMode = if (interrupt) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD
        val result = tts.speak(text, queueMode, null, utteranceId)
        if (result != TextToSpeech.SUCCESS) {
            return out.put("status", "error").put("message", "TTS engine rejected the utterance (speak() returned $result)")
        }

        CommandBus.post("voice -> \"$text\"${if (!languageOk) " (fallback language -- may sound wrong)" else ""}")
        return out.put("status", "ok").put("utterance_id", utteranceId).put("text", text).put("interrupt", interrupt)
    }

    /** Stops whatever's currently being said (and clears anything
     * queued behind it) without starting anything new -- distinct from
     * speak(text, interrupt=true), which stops-and-replaces. */
    fun stop(): JSONObject {
        val out = JSONObject()
        if (!ready) {
            return out.put("status", "error").put("message", initError ?: "TTS engine not ready yet")
        }
        tts.stop()
        speaking.clear()
        CommandBus.post("voice -> stop")
        return out.put("status", "ok")
    }

    fun statusJson(): JSONObject {
        val out = JSONObject()
        out.put("status", "ok")
        out.put("engine_ready", ready)
        out.put("speaking", speaking.isNotEmpty())
        out.put("language_ok", languageOk)
        out.put("voice", voiceName ?: JSONObject.NULL)
        if (initError != null) out.put("error", initError)
        return out
    }

    /** Sets the TTS engine's speech language, then upgrades to the
     * highest-quality voice actually available for it. Called
     * automatically once the engine finishes initializing (see the
     * onInit callback above) -- exposed as a separate function so it
     * can also be re-called later if voice ever needs to switch
     * languages at runtime. */
    fun configureLanguage(locale: Locale = Locale.US) {
        if (!ready) return
        val result = tts.setLanguage(locale)
        languageOk = result != TextToSpeech.LANG_MISSING_DATA && result != TextToSpeech.LANG_NOT_SUPPORTED
        if (!languageOk) {
            CommandBus.post("voice: x language $locale unavailable on this device (result=$result) -- using engine default")
            return
        }
        selectBestVoice(locale)
    }

    /** setLanguage() alone just picks SOME voice matching the locale --
     * not necessarily a good one. Modern Android TTS engines (Google's
     * default one especially) usually ship multiple voices per locale
     * at different quality tiers; this picks the best one actually
     * available, instead of settling for whatever the engine defaults
     * to. Deliberately excludes any voice with
     * isNetworkConnectionRequired=true: some of the engine's
     * highest-quality voices synthesize by calling out to Google's
     * servers, which would make speech quietly stop working the moment
     * the phone has LAN-to-the-PC but no real internet -- a new failure
     * mode this robot shouldn't have for a voice-quality upgrade. Falls
     * back to a same-language (any country) match if no exact locale
     * match exists; no-ops (keeps the engine's own default) if neither
     * is available, which is a normal outcome, not an error. */
    private fun selectBestVoice(locale: Locale) {
        val voices = try {
            tts.voices
        } catch (e: Exception) {
            CommandBus.post("voice: x couldn't enumerate voices (${e.message}) -- keeping engine default")
            null
        } ?: return
        val onDevice = voices.filter { !it.isNetworkConnectionRequired }
        val best = onDevice.filter { it.locale == locale }.maxByOrNull { it.quality }
            ?: onDevice.filter { it.locale.language == locale.language }.maxByOrNull { it.quality }

        if (best == null) {
            // Not an error -- this is the normal state on a phone that's
            // never had better voice data downloaded (Settings > System >
            // Languages & input > Text-to-speech output > gear icon >
            // Install voice data). Logged anyway because "did it look and
            // find nothing, or is something broken?" is genuinely
            // ambiguous from statusJson()'s voice=null alone -- this line
            // answers it without needing to guess.
            CommandBus.post(
                "voice: no on-device (offline) voice found for $locale " +
                    "(${voices.size} total, ${onDevice.size} offline-capable) -- " +
                    "using engine default; download voice data in system settings for a better one"
            )
            return
        }

        if (tts.setVoice(best) == TextToSpeech.SUCCESS) {
            voiceName = best.name
            CommandBus.post("voice: using \"${best.name}\" (quality=${best.quality}, on-device only)")
        }
    }

    fun shutdown() {
        tts.stop()
        tts.shutdown()
    }
}
