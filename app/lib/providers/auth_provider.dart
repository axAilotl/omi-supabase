import 'dart:async';
import 'dart:io';

import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/http/api/apps.dart' as apps_api;
import 'package:omi/backend/preferences.dart';
import 'package:omi/app_globals.dart';
import 'package:omi/providers/base_provider.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/services/notifications.dart';
import 'package:omi/utils/alerts/app_snackbar.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_manager.dart';
import 'package:omi/utils/platform/platform_service.dart';

class AuthenticationProvider extends BaseProvider {
  AppAuthUser? user;
  String? authToken;
  bool _loading = false;
  StreamSubscription<AppAuthUser?>? _authSubscription;

  @override
  bool get loading => _loading;

  AuthenticationProvider() {
    user = AuthService.instance.getCurrentUser();
    authToken = SharedPreferencesUtil().authToken.isEmpty ? null : SharedPreferencesUtil().authToken;
    _authSubscription = AuthService.instance.authStateChanges.listen((nextUser) {
      user = nextUser;
      authToken = SharedPreferencesUtil().authToken.isEmpty ? null : SharedPreferencesUtil().authToken;
      notifyListeners();
    });
  }

  @override
  void dispose() {
    _authSubscription?.cancel();
    super.dispose();
  }

  bool isSignedIn() {
    return AuthService.instance.isSignedIn();
  }

  void setLoading(bool value) {
    _loading = value;
    notifyListeners();
  }

  Future<void> onGoogleSignIn(Future<void> Function() onSignIn) async {
    if (loading) return;
    setLoadingState(true);
    try {
      final result = PlatformService.isMobile
          ? await AuthService.instance.signInWithGoogleMobile()
          : await AuthService.instance.authenticateWithProvider('google');
      if (result == null && isSignedIn()) {
        Logger.debug('Google OAuth returned null after session persistence; continuing with cached Supabase session');
      }
      if (result != null || isSignedIn()) {
        await _signIn(onSignIn);
      } else {
        AppSnackbar.showSnackbarError(
          globalNavigatorKey.currentContext?.l10n.authFailedToSignInWithGoogle ??
              'Failed to sign in with Google, please try again.',
        );
      }
    } catch (e, stackTrace) {
      Logger.debug('OAuth Google sign in error: $e');
      PlatformManager.instance.crashReporter.reportCrash(e, stackTrace);
      AppSnackbar.showSnackbarError(
        globalNavigatorKey.currentContext?.l10n.authenticationFailed ?? 'Authentication failed. Please try again.',
      );
    }
    setLoadingState(false);
  }

  Future<void> onAppleSignIn(Future<void> Function() onSignIn) async {
    if (loading) return;
    setLoadingState(true);
    try {
      final result = PlatformService.isMobile && !Platform.isAndroid
          ? await AuthService.instance.signInWithAppleMobile()
          : await AuthService.instance.authenticateWithProvider('apple');
      if (result == null && isSignedIn()) {
        Logger.debug('Apple OAuth returned null after session persistence; continuing with cached Supabase session');
      }
      if (result != null || isSignedIn()) {
        await _signIn(onSignIn);
      } else {
        AppSnackbar.showSnackbarError(
          globalNavigatorKey.currentContext?.l10n.authFailedToSignInWithApple ??
              'Failed to sign in with Apple, please try again.',
        );
      }
    } catch (e, stackTrace) {
      Logger.debug('OAuth Apple sign in error: $e');
      PlatformManager.instance.crashReporter.reportCrash(e, stackTrace);
      AppSnackbar.showSnackbarError(
        globalNavigatorKey.currentContext?.l10n.authenticationFailed ?? 'Authentication failed. Please try again.',
      );
    }
    setLoadingState(false);
  }

  Future<String?> _getIdToken() async {
    try {
      final token = await AuthService.instance.getIdToken();
      if (token == null) return null;

      try {
        NotificationService.instance.saveNotificationToken();
      } catch (e, stackTrace) {
        Logger.debug('Notification token save failed after sign-in: $e');
        PlatformManager.instance.crashReporter.reportCrash(e, stackTrace);
      }

      return token;
    } catch (e, stackTrace) {
      AppSnackbar.showSnackbarError(
        globalNavigatorKey.currentContext?.l10n.authenticationFailed ?? 'Authentication failed. Please try again.',
      );
      PlatformManager.instance.crashReporter.reportCrash(e, stackTrace);
      return null;
    }
  }

  Future<void> _signIn(Future<void> Function() onSignIn) async {
    final token = await _getIdToken();
    if (token == null) {
      AppSnackbar.showSnackbarError(
        globalNavigatorKey.currentContext?.l10n.authUnexpectedError ?? 'Unexpected error signing in, please try again',
      );
      return;
    }

    final currentUser = AuthService.instance.getCurrentUser();
    if (currentUser == null) {
      AppSnackbar.showSnackbarError(
        globalNavigatorKey.currentContext?.l10n.authUnexpectedError ?? 'Unexpected error signing in, please try again',
      );
      return;
    }

    user = currentUser;
    authToken = token;
    SharedPreferencesUtil().uid = currentUser.uid;
    MixpanelManager().identify();
    notifyListeners();
    try {
      await onSignIn();
    } catch (e, stackTrace) {
      Logger.debug('Post sign-in flow failed after auth succeeded: $e');
      PlatformManager.instance.crashReporter.reportCrash(e, stackTrace);
    }
  }

  void openTermsOfService() {
    _launchUrl('https://www.omi.me/pages/terms-of-service');
  }

  void openPrivacyPolicy() {
    _launchUrl('https://www.omi.me/pages/privacy');
  }

  void _launchUrl(String url) async {
    final uri = Uri.tryParse(url);
    if (uri == null) {
      Logger.debug('Invalid URL');
      return;
    }

    await launchUrl(uri, mode: LaunchMode.inAppBrowserView);
  }

  Future<void> linkWithGoogle() async {
    setLoading(true);
    try {
      final oldUserId = SharedPreferencesUtil().uid;
      final result = await AuthService.instance.linkWithGoogle();
      if (result == null) return;
      final newUserId = result.user.uid;
      if (oldUserId.isNotEmpty && newUserId.isNotEmpty && oldUserId != newUserId) {
        SharedPreferencesUtil().onboardingCompleted = false;
        await apps_api.migrateAppOwnerId(oldUserId);
      }
      user = result.user;
      notifyListeners();
    } catch (e) {
      AppSnackbar.showSnackbarError(
        globalNavigatorKey.currentContext?.l10n.authFailedToLinkGoogle ??
            'Failed to link with Google, please try again.',
      );
      rethrow;
    } finally {
      setLoading(false);
    }
  }

  Future<void> linkWithApple() async {
    setLoading(true);
    try {
      final oldUserId = SharedPreferencesUtil().uid;
      final result = await AuthService.instance.linkWithApple();
      if (result == null) return;
      final newUserId = result.user.uid;
      if (oldUserId.isNotEmpty && newUserId.isNotEmpty && oldUserId != newUserId) {
        SharedPreferencesUtil().onboardingCompleted = false;
        await apps_api.migrateAppOwnerId(oldUserId);
      }
      user = result.user;
      notifyListeners();
    } catch (e) {
      Logger.debug('Error linking with Apple: $e');
      AppSnackbar.showSnackbarError(
        globalNavigatorKey.currentContext?.l10n.authFailedToLinkApple ?? 'Failed to link with Apple, please try again.',
      );
      rethrow;
    } finally {
      setLoading(false);
    }
  }
}
