package com.legobot.phoneprobe

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.camera2.*
import android.media.Image
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import kotlin.coroutines.resume

/**
 * Proof-of-life for camera access, standing in for media_relay.py's
 * cv2.VideoCapture (which needs a /dev/video* V4L2 node -- Android has
 * no such thing; Camera2 is the real API underneath it). Captures
 * exactly one JPEG to prove the permission + hardware path works, and
 * saves it to app-private storage rather than trying to stream
 * anything yet -- streaming is a separate, harder problem (frame rate,
 * backpressure, the WebSocket framing lego_pi's media_relay.py already
 * defines) that only matters once single-shot capture is proven.
 */
class CameraProbe(private val context: Context) {

    @SuppressLint("MissingPermission") // caller (ProbeService) verifies CAMERA permission first
    suspend fun captureOne(): JSONObject = suspendCancellableCoroutine { cont ->
        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val cameraId = manager.cameraIdList.firstOrNull { id ->
            manager.getCameraCharacteristics(id)
                .get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_FRONT
        } ?: manager.cameraIdList.firstOrNull()

        if (cameraId == null) {
            cont.resume(errorResult("No camera found on this device."))
            return@suspendCancellableCoroutine
        }

        val thread = HandlerThread("CameraProbeThread").also { it.start() }
        val handler = Handler(thread.looper)
        var deviceRef: CameraDevice? = null

        // Only close the device once capture has actually finished (success
        // or failure) -- closing it right after createCaptureSession() was
        // called (which is async) used to yank the camera out from under
        // its own in-flight session and crash the process.
        fun cleanupAndResume(result: JSONObject) {
            try { deviceRef?.close() } catch (_: Exception) {}
            thread.quitSafely()
            if (cont.isActive) cont.resume(result)
        }

        val chars = manager.getCameraCharacteristics(cameraId)
        val sizes = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            ?.getOutputSizes(android.graphics.ImageFormat.JPEG)
        val size = sizes?.firstOrNull() ?: android.util.Size(640, 480)

        val reader = ImageReader.newInstance(size.width, size.height, android.graphics.ImageFormat.JPEG, 1)

        reader.setOnImageAvailableListener({ r ->
            val image: Image? = r.acquireLatestImage()
            try {
                if (image == null) {
                    cleanupAndResume(errorResult("ImageReader produced no image."))
                    return@setOnImageAvailableListener
                }
                val buffer = image.planes[0].buffer
                val bytes = ByteArray(buffer.remaining())
                buffer.get(bytes)

                val outFile = File(context.filesDir, "probe_capture.jpg")
                FileOutputStream(outFile).use { it.write(bytes) }

                val out = JSONObject()
                out.put("status", "ok")
                out.put("width", size.width)
                out.put("height", size.height)
                out.put("bytes", bytes.size)
                out.put("saved_to", outFile.absolutePath)
                cleanupAndResume(out)
            } finally {
                image?.close()
            }
        }, handler)

        manager.openCamera(cameraId, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                deviceRef = camera
                try {
                    val captureRequest = camera.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE)
                    captureRequest.addTarget(reader.surface)

                    camera.createCaptureSession(
                        listOf(reader.surface),
                        object : CameraCaptureSession.StateCallback() {
                            override fun onConfigured(session: CameraCaptureSession) {
                                session.capture(captureRequest.build(), null, handler)
                            }
                            override fun onConfigureFailed(session: CameraCaptureSession) {
                                cleanupAndResume(errorResult("Camera capture session config failed."))
                            }
                        },
                        handler
                    )
                } catch (e: Exception) {
                    cleanupAndResume(errorResult("Capture setup threw: ${e.message}"))
                }
            }

            override fun onDisconnected(camera: CameraDevice) {
                cleanupAndResume(errorResult("Camera disconnected before capture finished."))
            }

            override fun onError(camera: CameraDevice, error: Int) {
                cleanupAndResume(errorResult("Camera device error code $error."))
            }
        }, handler)
    }

    private fun errorResult(message: String): JSONObject {
        val out = JSONObject()
        out.put("status", "error")
        out.put("message", message)
        return out
    }
}
