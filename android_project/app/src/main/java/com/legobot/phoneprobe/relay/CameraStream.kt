package com.legobot.phoneprobe.relay

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.media.Image
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import java.io.ByteArrayOutputStream

private const val VIDEO_FPS = 5
private const val JPEG_QUALITY = 70
private const val MIN_FRAME_INTERVAL_MS = 1000L / VIDEO_FPS

/**
 * Continuous equivalent of CameraProbe.kt's single-shot capture, same
 * reasoning as media_relay.py's `_video_loop`: opens the camera ONCE
 * and keeps a repeating preview-style request running for the whole
 * service lifetime, rather than open/capture/close per frame (which is
 * both slow and exactly the pattern that caused the earlier
 * open/close-ordering bug in CameraProbe).
 *
 * Uses YUV_420_888 (the only format Camera2 reliably supports as a
 * *repeating* target -- JPEG output surfaces are meant for single
 * still captures, not continuous streams) and converts to JPEG in
 * software via YuvImage, throttled to VIDEO_FPS so the conversion
 * doesn't run at full sensor rate for no reason.
 */
class CameraStream(private val context: Context) {

    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var lastEmitMs = 0L

    // How many degrees clockwise the raw sensor frame needs rotating to
    // appear upright -- see start()'s computation of this. Unlike
    // CameraProbe.kt's single JPEG capture (which can lean on Camera2's
    // built-in JPEG_ORIENTATION/EXIF mechanism), this class hand-builds
    // JPEGs from raw YUV via YuvImage.compressToJpeg(), which has no
    // orientation support at all -- whatever pixels go in come out
    // exactly as given, so the rotation has to be applied to the actual
    // pixel data ourselves (see rotateNv21 below).
    private var rotationDegrees = 0

    @SuppressLint("MissingPermission") // caller verifies CAMERA permission first
    fun start() {
        val ht = HandlerThread("CameraStreamThread").also { it.start() }
        thread = ht
        val h = Handler(ht.looper)
        handler = h

        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val cameraId = manager.cameraIdList.firstOrNull { id ->
            manager.getCameraCharacteristics(id)
                .get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_FRONT
        } ?: manager.cameraIdList.firstOrNull() ?: return

        // Same formula as CameraProbe.kt's JPEG_ORIENTATION computation
        // (Android's documented front-camera formula, degenerate/equal to
        // the back-camera formula when deviceOrientationDegrees=0, which
        // it always is here -- see CameraProbe.kt's comment for why that's
        // a guaranteed invariant rather than an assumption). Only
        // multiples of 90 are handled by rotateNv21 below; anything else
        // reported by a camera would be unusual hardware and gets clamped
        // to the nearest 90 rather than silently doing nothing.
        val sensorOrientation = manager.getCameraCharacteristics(cameraId)
            .get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0
        rotationDegrees = ((sensorOrientation % 360) + 360) % 360 / 90 * 90

        val size = android.util.Size(640, 480)
        val r = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 2)
        reader = r
        r.setOnImageAvailableListener({ imgReader ->
            val image = imgReader.acquireLatestImage()
            if (image != null) {
                try {
                    maybeEmit(image)
                } finally {
                    image.close()
                }
            }
        }, h)

        manager.openCamera(cameraId, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                cameraDevice = camera
                try {
                    val request = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW)
                    request.addTarget(r.surface)
                    camera.createCaptureSession(
                        listOf(r.surface),
                        object : CameraCaptureSession.StateCallback() {
                            override fun onConfigured(session: CameraCaptureSession) {
                                captureSession = session
                                session.setRepeatingRequest(request.build(), null, h)
                            }
                            override fun onConfigureFailed(session: CameraCaptureSession) {
                                // Nothing running yet to tear down beyond the device itself.
                            }
                        },
                        h
                    )
                } catch (e: Exception) {
                    // Leave cleanup to stop() -- caller decides whether a camera
                    // failure should take the whole relay down or just skip video.
                }
            }

            override fun onDisconnected(camera: CameraDevice) {
                camera.close()
            }

            override fun onError(camera: CameraDevice, error: Int) {
                camera.close()
            }
        }, h)
    }

    private fun maybeEmit(image: Image) {
        val now = System.currentTimeMillis()
        if (now - lastEmitMs < MIN_FRAME_INTERVAL_MS) return
        lastEmitMs = now

        var nv21 = yuv420ToNv21(image)
        var width = image.width
        var height = image.height
        if (rotationDegrees != 0) {
            val (rotated, w, h) = rotateNv21(nv21, width, height, rotationDegrees)
            nv21 = rotated
            width = w
            height = h
        }

        val yuvImage = YuvImage(nv21, ImageFormat.NV21, width, height, null)
        val out = ByteArrayOutputStream()
        yuvImage.compressToJpeg(Rect(0, 0, width, height), JPEG_QUALITY, out)
        MediaHub.emit(TYPE_VIDEO, out.toByteArray())
    }

    private fun yuv420ToNv21(image: Image): ByteArray {
        // Standard YUV_420_888 (3-plane, possibly with row/pixel stride
        // padding) -> NV21 (interleaved VU, no padding) repacking.
        // YuvImage.compressToJpeg only understands NV21, not the raw
        // plane layout Camera2 hands back.
        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        val width = image.width
        val height = image.height
        val nv21 = ByteArray(width * height * 3 / 2)

        var pos = 0
        val yRowStride = yPlane.rowStride
        val yBuffer = yPlane.buffer
        for (row in 0 until height) {
            yBuffer.position(row * yRowStride)
            yBuffer.get(nv21, pos, width)
            pos += width
        }

        val uvRowStride = vPlane.rowStride
        val uvPixelStride = vPlane.pixelStride
        val vBuffer = vPlane.buffer
        val uBuffer = uPlane.buffer
        val chromaHeight = height / 2
        val chromaWidth = width / 2
        for (row in 0 until chromaHeight) {
            for (col in 0 until chromaWidth) {
                val vIndex = row * uvRowStride + col * uvPixelStride
                val uIndex = row * uvRowStride + col * uvPixelStride
                nv21[pos++] = vBuffer.get(vIndex) // NV21 order: V then U
                nv21[pos++] = uBuffer.get(uIndex)
            }
        }
        return nv21
    }

    /** Rotates an NV21 buffer by 90/180/270 degrees CLOCKWISE, returning
     * the rotated bytes plus the new (possibly swapped) width/height.
     * Index formulas below were derived from scratch and verified against
     * numpy's rot90 (documented, unambiguous rotation direction) on
     * non-square test grids -- worth noting because a commonly-copied
     * "classic" NV21-rotation snippet that looks superficially similar to
     * this was checked the same way first and turned out to be wrong
     * (didn't match ground truth on any of 90/180/270), so this was
     * re-derived rather than trusted from memory. rotation=0 returns the
     * input unchanged; anything not a multiple of 90 is the caller's bug
     * (rotationDegrees above only ever produces 0/90/180/270). */
    private fun rotateNv21(nv21: ByteArray, width: Int, height: Int, rotation: Int): Triple<ByteArray, Int, Int> {
        if (rotation == 0) return Triple(nv21, width, height)
        require(rotation == 90 || rotation == 180 || rotation == 270) { "rotation must be 0/90/180/270, got $rotation" }

        val frameSize = width * height
        val outWidth = if (rotation == 180) width else height
        val outHeight = if (rotation == 180) height else width
        val out = ByteArray(nv21.size)

        fun rotatedIndex(i: Int, j: Int, w: Int, h: Int): Int = when (rotation) {
            90 -> i * h + (h - 1 - j)
            180 -> (h - 1 - j) * w + (w - 1 - i)
            else -> (w - 1 - i) * h + j // 270
        }

        for (j in 0 until height) {
            for (i in 0 until width) {
                out[rotatedIndex(i, j, width, height)] = nv21[j * width + i]
            }
        }

        val chromaWidth = width / 2
        val chromaHeight = height / 2
        for (j in 0 until chromaHeight) {
            for (i in 0 until chromaWidth) {
                val oldOffset = frameSize + (j * chromaWidth + i) * 2
                val newOffset = frameSize + rotatedIndex(i, j, chromaWidth, chromaHeight) * 2
                out[newOffset] = nv21[oldOffset]         // V
                out[newOffset + 1] = nv21[oldOffset + 1] // U
            }
        }

        return Triple(out, outWidth, outHeight)
    }

    fun stop() {
        try { captureSession?.close() } catch (_: Exception) {}
        try { cameraDevice?.close() } catch (_: Exception) {}
        try { reader?.close() } catch (_: Exception) {}
        thread?.quitSafely()
        captureSession = null
        cameraDevice = null
        reader = null
        thread = null
        handler = null
    }
}
