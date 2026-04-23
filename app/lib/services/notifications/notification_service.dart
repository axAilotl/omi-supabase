// Platform-aware notification service.
// Full Supabase mode uses local notifications only and skips remote push setup.

import 'package:omi/services/notifications/notification_interface.dart';
import 'package:omi/services/notifications/notification_service_basic.dart' as basic;

/// Factory function to create the notification service
NotificationInterface _createPlatformNotificationService() {
  return basic.createNotificationService();
}

/// Singleton notification service instance
/// Automatically selects the correct platform-specific implementation
class NotificationService {
  static NotificationInterface? _instance;

  /// Get the singleton notification service instance
  static NotificationInterface get instance {
    _instance ??= _createPlatformNotificationService();
    return _instance!;
  }

  /// Clear the instance (useful for testing)
  static void reset() {
    _instance = null;
  }
}
