package com.legobot.phoneprobe.relay

import io.ktor.server.application.install
import io.ktor.server.cio.CIO
import io.ktor.server.engine.ApplicationEngine
import io.ktor.server.engine.embeddedServer
import io.ktor.server.routing.routing
import io.ktor.server.websocket.WebSockets
import io.ktor.server.websocket.webSocket
import io.ktor.websocket.Frame
import io.ktor.websocket.close
import kotlinx.coroutines.flow.collect

private const val PORT = 8001

/**
 * Same port and path as lego_pi/media_relay.py (8001, "/media") on
 * purpose -- lego_brain/media_client.py connects to
 * ws://<host>:8001/media and doesn't care whether <host> is the Pi or
 * this phone. Deliberately separate from ProbeService's port 8765:
 * same reasoning as the Pi split (8000 command / 8001 media) -- a
 * slow/blocked media connection should never share a listener with
 * anything control-related.
 */
class MediaRelayServer {
    // ApplicationEngine, NOT EmbeddedServer -- that wrapper class is a
    // Ktor 3.x type. embeddedServer() returns the engine directly in
    // the 2.3.x line this project depends on.
    private var server: ApplicationEngine? = null

    fun start() {
        server = embeddedServer(CIO, port = PORT) {
            install(WebSockets)
            routing {
                webSocket("/media") {
                    try {
                        MediaHub.frames.collect { packed ->
                            send(Frame.Binary(true, packed))
                        }
                    } catch (e: Exception) {
                        close()
                    }
                }
            }
        }.start(wait = false)
    }

    fun stop() {
        server?.stop(gracePeriodMillis = 200, timeoutMillis = 500)
        server = null
    }
}
