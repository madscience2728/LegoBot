package com.legobot.phoneprobe

import android.os.Handler
import android.os.Looper

/**
 * Tiny in-process pub/sub so ProbeService's background HTTP thread can
 * push human-readable lines up to MainActivity's on-screen console
 * without any Android IPC machinery -- everything here runs in the same
 * process, so a plain singleton is enough. MainActivity subscribes
 * while it's actually visible (onStart/onStop) and unsubscribes
 * otherwise, so nothing holds an Activity reference while backgrounded.
 *
 * Deliberately just strings, not structured events -- the console's
 * whole job right now is "show that something happened, and what the
 * probe stage did about it (nothing yet)". Revisit if the UI ever needs
 * to react to specific fields instead of just displaying lines.
 */
object CommandBus {
    private val mainHandler = Handler(Looper.getMainLooper())
    private var listener: ((String) -> Unit)? = null

    fun subscribe(cb: (String) -> Unit) {
        listener = cb
    }

    fun unsubscribe() {
        listener = null
    }

    /** Safe to call from any thread (NanoHTTPD serves each request on
     * its own worker thread) -- always delivers on the main thread. */
    fun post(line: String) {
        mainHandler.post { listener?.invoke(line) }
    }
}
