package com.legobot.phoneprobe

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * The phone has exactly one BLE radio. BleScanner's periodic
 * Devices-Seen scan (started every ~15s from the PC dashboard) and
 * HubConnector's connect-time scan (started when someone clicks
 * Connect on a hub) both call BluetoothLeScanner.startScan()
 * independently -- if both land at the same moment, Android can reject
 * one outright (SCAN_FAILED_ALREADY_STARTED) or throttle it after
 * repeated start/stop cycles in a short window
 * (SCAN_FAILED_APPLICATION_REGISTRATION_FAILED). Before this existed,
 * that failure was ALSO being silently swallowed by a dead-code bug in
 * onScanFailed, so it looked like "scanned fine, found nothing" instead
 * of a real error -- see BleScanner.kt's fixed onScanFailed for the
 * other half of this fix.
 *
 * This mutex just makes the two callers take turns instead of racing.
 * Cheap insurance even once the onScanFailed bug is fixed, since the
 * error would still be real and still cost you a failed scan/connect
 * attempt -- better to not collide at all.
 */
object BleScanLock {
    private val mutex = Mutex()

    suspend fun <T> withLock(block: suspend () -> T): T = mutex.withLock { block() }
}
