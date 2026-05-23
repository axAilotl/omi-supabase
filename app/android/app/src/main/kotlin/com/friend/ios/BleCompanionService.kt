package com.friend.ios

import android.companion.AssociationInfo
import android.companion.CompanionDeviceManager
import android.companion.CompanionDeviceService
import android.companion.DevicePresenceEvent
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import java.util.Locale

/**
 * CompanionDeviceService that receives device appear/disappear events from the OS,
 * even when the app is not running.
 *
 * Presence is allowed to restore the native BLE foreground service without a
 * Flutter engine. User pause/stop intent is persisted separately and suppresses
 * companion auto-start until the user reconnects or resumes.
 */
class BleCompanionService : CompanionDeviceService() {

    companion object {
        private const val TAG = "OmiBle.CompanionSvc"

        @Volatile
        private var appearCount = 0
    }

    private fun hasBluetoothPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            applicationContext, "android.permission.BLUETOOTH_CONNECT"
        ) == 0
    }

    private fun findAssociation(assocId: Int): AssociationInfo? {
        val cdm = getSystemService("companiondevice") as? CompanionDeviceManager ?: return null
        return cdm.myAssociations.find { it.id == assocId }
    }

    private fun handleDeviceAppeared(address: String) {
        Log.i(TAG, "Device appeared: $address")

        if (!hasBluetoothPermission()) return
        if (OmiBleLifecycleStore.companionAutoStartSuppressed(applicationContext)) {
            Log.i(TAG, "Device appeared but user intent suppresses companion auto-start")
            return
        }

        val saved = OmiBleLifecycleStore.managedDevice(applicationContext)
        val requiresBond = saved?.takeIf {
            it.address.equals(address, ignoreCase = true)
        }?.requiresBond ?: false

        OmiBleForegroundService.startService(
            applicationContext, address,
            requiresBond = requiresBond,
            caller = "CompanionSvc.deviceAppeared"
        )
    }

    private fun handleDeviceDisappeared() {
        Log.i(TAG, "Device disappeared")
    }

    // ---- Lifecycle ----

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "onCreate")

        if (!hasBluetoothPermission()) return
        if (OmiBleLifecycleStore.companionAutoStartSuppressed(applicationContext)) {
            Log.i(TAG, "onCreate fallback suppressed by user intent")
            return
        }

        val saved = OmiBleLifecycleStore.managedDevice(applicationContext) ?: return
        OmiBleForegroundService.startService(
            applicationContext, saved.address,
            requiresBond = saved.requiresBond,
            caller = "CompanionSvc.onCreateFallback"
        )
    }

    // ---- API 31-35: String-based callbacks ----

    override fun onDeviceAppeared(address: String) {
        super.onDeviceAppeared(address)
        if (Build.VERSION.SDK_INT >= 36) return

        if (OmiBleForegroundService.isActive()) {
            appearCount++
            return
        }

        appearCount = 1
        handleDeviceAppeared(address)
    }

    override fun onDeviceDisappeared(address: String) {
        super.onDeviceDisappeared(address)
        if (Build.VERSION.SDK_INT >= 36) return

        appearCount--
        if (appearCount == 0) {
            handleDeviceDisappeared()
        }
    }

    // ---- API 36+: DevicePresenceEvent-based callback ----

    override fun onDevicePresenceEvent(event: DevicePresenceEvent) {
        val association = findAssociation(event.associationId)
        val address = association?.deviceMacAddress?.toString()?.uppercase(Locale.ROOT)

        when (event.event) {
            DevicePresenceEvent.EVENT_BLE_APPEARED -> {
                if (address != null) handleDeviceAppeared(address)
            }
            DevicePresenceEvent.EVENT_BLE_DISAPPEARED -> {
                handleDeviceDisappeared()
            }
        }

        super.onDevicePresenceEvent(event)
    }
}
