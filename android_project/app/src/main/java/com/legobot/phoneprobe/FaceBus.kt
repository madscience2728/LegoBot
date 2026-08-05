package com.legobot.phoneprobe

import android.os.Handler
import android.os.Looper

/**
 * Tiny in-process pub/sub so ProbeService's background HTTP thread can
 * push a new face onto MainActivity's on-screen emojiFace TextView --
 * same reasoning and same shape as CommandBus, just for the robot's
 * expression instead of the scrolling log. See MainActivity's own doc
 * comment for the "emoji face (center, placeholder for a future
 * robot-state expression)" this was always meant to become.
 *
 * Two ways in, same as HubConnector's LED color command ended up
 * needing: a small fixed vocabulary of named expressions (EXPRESSIONS --
 * the reliable path a future LLM's tool-call should use, since a fixed
 * enum is a far smaller/safer surface than "emit arbitrary Unicode and
 * hope it renders"), and a raw emoji override for anything that
 * vocabulary doesn't cover yet.
 */
object FaceBus {

    val EXPRESSIONS: Map<String, String> = mapOf(
        "neutral" to "\uD83D\uDE42",     // 🙂
        "happy" to "\uD83D\uDE04",       // 😄
        "excited" to "\uD83E\uDD29",     // 🤩
        "curious" to "\uD83E\uDD14",     // 🤔
        "confused" to "\uD83D\uDE15",    // 😕
        "sad" to "\uD83D\uDE41",         // 🙁
        "sleepy" to "\uD83D\uDE34",      // 😴
        "surprised" to "\uD83D\uDE2E",   // 😮
        "listening" to "\uD83D\uDC42",   // 👂
        "speaking" to "\uD83D\uDDE3\uFE0F", // 🗣️
        "error" to "\uD83D\uDE35",       // 😵
        "off" to "\u2B1B",               // ⬛
    )

    // Current face, kept here (not just broadcast) so a fresh subscriber
    // -- MainActivity re-created after rotation, or subscribing for the
    // first time -- shows the actual current expression immediately
    // instead of a stale default until the next change arrives.
    @Volatile var current: String = EXPRESSIONS.getValue("neutral")
        private set

    private val mainHandler = Handler(Looper.getMainLooper())
    private var listener: ((String) -> Unit)? = null

    /** Immediately delivers the current face to the new subscriber, then
     * every change after that -- unlike CommandBus.subscribe(), which is
     * pure future-only, because a face is current STATE (what does the
     * robot look like right now), not a log of discrete past events. */
    fun subscribe(cb: (String) -> Unit) {
        listener = cb
        cb(current)
    }

    fun unsubscribe() {
        listener = null
    }

    /** Safe to call from any thread (NanoHTTPD serves each request on
     * its own worker thread) -- always delivers on the main thread. */
    fun setEmoji(emoji: String) {
        current = emoji
        mainHandler.post { listener?.invoke(emoji) }
    }

    /** Looks up a named expression and applies it. Returns the resolved
     * emoji on success, null if the name isn't in EXPRESSIONS -- callers
     * (ProbeService) turn a null into a proper error response rather
     * than silently falling back to something. */
    fun setExpression(name: String): String? {
        val emoji = EXPRESSIONS[name.lowercase()] ?: return null
        setEmoji(emoji)
        return emoji
    }
}
