import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'package:omi/utils/debugging/crash_reporter.dart';

class CrashReporterManager implements CrashReporter {
  static final CrashReporterManager _instance = CrashReporterManager._internal();
  static CrashReporterManager get instance => _instance;

  CrashReporterManager._internal();

  factory CrashReporterManager() {
    return _instance;
  }

  static Future<void> init() async {
    debugPrint('Crash reporter initialized in local mode (debug=$kDebugMode)');
  }

  @override
  void identifyUser(String email, String name, String userId) {}

  @override
  void logInfo(String message) {}

  @override
  void logError(String message) {}

  @override
  void logWarn(String message) {}

  @override
  void logDebug(String message) {}

  @override
  void logVerbose(String message) {}

  @override
  void setUserAttribute(String key, String value) {}

  @override
  void setEnabled(bool isEnabled) {}

  @override
  Future<void> reportCrash(Object exception, StackTrace stackTrace, {Map<String, String>? userAttributes}) async {
    debugPrint('Crash report: $exception');
    debugPrint('$stackTrace');
    if (userAttributes == null || userAttributes.isEmpty) return;
    for (final entry in userAttributes.entries) {
      debugPrint('Crash attribute ${entry.key}=${entry.value}');
    }
  }

  @override
  NavigatorObserver? getNavigatorObserver() {
    return null;
  }

  @override
  bool get isSupported => false;
}
