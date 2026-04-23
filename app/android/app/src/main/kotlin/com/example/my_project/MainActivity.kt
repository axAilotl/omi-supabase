package com.friend.ios

import android.content.Intent
import androidx.annotation.NonNull
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity: FlutterActivity() {
    private val CHANNEL = "com.friend.ios/notifyOnKill"
    private val DEEP_LINK_CHANNEL = "com.omi/deep_links"
    private var bleHostApiImpl: BleHostApiImpl? = null
    private var deepLinkChannel: MethodChannel? = null

    override fun configureFlutterEngine(@NonNull flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        // Register WiFi Network Plugin
        WifiNetworkPlugin.registerWith(flutterEngine, this)

        // Register Phone Calls Plugin
        PhoneCallsPlugin.registerWith(flutterEngine, this)

        // Register Native BLE Pigeon APIs
        OmiBleManager.initialize(application)
        OmiBleManager.isFlutterAlive = true
        OmiBleManager.instance.flutterApi = BleFlutterApi(flutterEngine.dartExecutor.binaryMessenger)
        val hostApi = BleHostApiImpl { this }
        hostApi.initCompanionManager(this)
        bleHostApiImpl = hostApi
        BleHostApi.setUp(flutterEngine.dartExecutor.binaryMessenger, hostApi)
        deepLinkChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, DEEP_LINK_CHANNEL)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL).setMethodCallHandler {
            call, result ->
            if(call.method == "setNotificationOnKillService"){
                 val title = call.argument<String>("title")
                val description = call.argument<String>("description")

                val serviceIntent = Intent(this, NotificationOnKillService::class.java)

                serviceIntent.putExtra("title", title)
                serviceIntent.putExtra("description", description)

                startService(serviceIntent)
                result.success(true)
            }else{
                result.notImplemented()
            }
        }

        handleDeepLink(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleDeepLink(intent)
    }

    private fun handleDeepLink(intent: Intent?) {
        val url = intent?.dataString ?: return
        deepLinkChannel?.invokeMethod("onDeepLink", url)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        // Handle CompanionDeviceManager chooser result
        val address = bleHostApiImpl?.onActivityResult(requestCode, resultCode, data)
        if (address != null) {
            // Device selected — start foreground service (Dart will call manageDevice)
            OmiBleForegroundService.startService(this, address, caller = "MainActivity.onActivityResult")
        }
    }

    override fun onResume() {
        super.onResume()
        OmiBleManager.isAppForeground = true
    }

    override fun onPause() {
        OmiBleManager.isAppForeground = false
        super.onPause()
    }

    override fun onDestroy() {
        // When user closes the app (swipe away), stop the foreground service.
        // The service handles disconnecting all managed devices in onDestroy.
        if (isFinishing) {
            OmiBleManager.isFlutterAlive = false
            OmiBleForegroundService.stopService(this)
        }
        super.onDestroy()
    }
}
