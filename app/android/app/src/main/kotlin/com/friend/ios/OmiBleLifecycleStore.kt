package com.friend.ios

import android.content.Context
import java.util.Locale

data class OmiBleManagedDevice(
    val address: String,
    val requiresBond: Boolean
)

data class OmiBleLifecycleSnapshot(
    val managedDevice: OmiBleManagedDevice?,
    val userDisconnected: Boolean,
    val userPaused: Boolean
) {
    val companionAutoStartSuppressed: Boolean
        get() = userDisconnected || userPaused

    val userState: String
        get() = when {
            userPaused -> "paused"
            userDisconnected -> "stopped"
            else -> "active"
        }
}

object OmiBleLifecycleStore {
    private const val PREFS_NAME = "ble_config"
    private const val PREFS_KEY_MANAGED_DEVICE = "managed_device"
    private const val PREFS_KEY_USER_DISCONNECTED = "user_disconnected"
    private const val PREFS_KEY_USER_PAUSED = "user_paused"

    fun snapshot(context: Context): OmiBleLifecycleSnapshot {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return OmiBleLifecycleSnapshot(
            managedDevice = parseManagedDevice(prefs.getString(PREFS_KEY_MANAGED_DEVICE, null)),
            userDisconnected = prefs.getBoolean(PREFS_KEY_USER_DISCONNECTED, false),
            userPaused = prefs.getBoolean(PREFS_KEY_USER_PAUSED, false)
        )
    }

    fun managedDevice(context: Context): OmiBleManagedDevice? = snapshot(context).managedDevice

    fun companionAutoStartSuppressed(context: Context): Boolean {
        return snapshot(context).companionAutoStartSuppressed
    }

    fun saveManagedDevice(context: Context, address: String, requiresBond: Boolean) {
        val normalizedAddress = address.uppercase(Locale.ROOT)
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putString(PREFS_KEY_MANAGED_DEVICE, "$normalizedAddress|$requiresBond")
            .putBoolean(PREFS_KEY_USER_DISCONNECTED, false)
            .putBoolean(PREFS_KEY_USER_PAUSED, false)
            .apply()
    }

    fun markUserDisconnected(context: Context) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putBoolean(PREFS_KEY_USER_DISCONNECTED, true)
            .putBoolean(PREFS_KEY_USER_PAUSED, false)
            .apply()
    }

    fun markUserPaused(context: Context) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putBoolean(PREFS_KEY_USER_PAUSED, true)
            .putBoolean(PREFS_KEY_USER_DISCONNECTED, false)
            .apply()
    }

    fun markUserResumed(context: Context) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putBoolean(PREFS_KEY_USER_DISCONNECTED, false)
            .putBoolean(PREFS_KEY_USER_PAUSED, false)
            .apply()
    }

    private fun parseManagedDevice(raw: String?): OmiBleManagedDevice? {
        if (raw.isNullOrBlank()) return null
        val parts = raw.split("|")
        if (parts.isEmpty() || parts[0].isBlank()) return null

        return OmiBleManagedDevice(
            address = parts[0].uppercase(Locale.ROOT),
            requiresBond = parts.getOrNull(1)?.equals("true", ignoreCase = true) ?: false
        )
    }
}
