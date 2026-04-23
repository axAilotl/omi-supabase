import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:app_links/app_links.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

class AppAuthUser {
  final String uid;
  final String? email;
  final String? displayName;
  final String? givenName;
  final String? familyName;

  const AppAuthUser({
    required this.uid,
    this.email,
    this.displayName,
    this.givenName,
    this.familyName,
  });
}

class AuthResult {
  final AppAuthUser user;
  final String accessToken;
  final String refreshToken;
  final int expiresIn;
  final String provider;

  const AuthResult({
    required this.user,
    required this.accessToken,
    required this.refreshToken,
    required this.expiresIn,
    required this.provider,
  });
}

class AuthService {
  static final AuthService _instance = AuthService._internal();
  static AuthService get instance => _instance;

  AuthService._internal();

  static const _deepLinkChannel = MethodChannel('com.omi/deep_links');

  final StreamController<AppAuthUser?> _authStateController = StreamController<AppAuthUser?>.broadcast();

  Stream<AppAuthUser?> get authStateChanges => _authStateController.stream;

  bool isSignedIn() {
    return SharedPreferencesUtil().uid.isNotEmpty && SharedPreferencesUtil().refreshToken.isNotEmpty;
  }

  AppAuthUser? getCurrentUser() {
    final uid = SharedPreferencesUtil().uid;
    if (uid.isEmpty) return null;
    final givenName = SharedPreferencesUtil().givenName;
    final familyName = SharedPreferencesUtil().familyName;
    final displayName = [givenName, familyName].where((value) => value.isNotEmpty).join(' ').trim();
    return AppAuthUser(
      uid: uid,
      email: SharedPreferencesUtil().email.isEmpty ? null : SharedPreferencesUtil().email,
      displayName: displayName.isEmpty ? null : displayName,
      givenName: givenName.isEmpty ? null : givenName,
      familyName: familyName.isEmpty ? null : familyName,
    );
  }

  Future<AuthResult?> signInWithGoogleMobile() async {
    return authenticateWithProvider('google');
  }

  Future<AuthResult?> signInWithAppleMobile() async {
    return authenticateWithProvider('apple');
  }

  Future<void> signOut() async {
    _clearCachedAuth();
    _authStateController.add(null);
  }

  void _clearCachedAuth() {
    SharedPreferencesUtil().authToken = '';
    SharedPreferencesUtil().refreshToken = '';
    SharedPreferencesUtil().tokenExpirationTime = 0;
    SharedPreferencesUtil().uid = '';
    SharedPreferencesUtil().email = '';
    SharedPreferencesUtil().givenName = '';
    SharedPreferencesUtil().familyName = '';
  }

  Future<String?> getIdToken() async {
    if (!isSignedIn()) {
      _clearCachedAuth();
      return null;
    }

    final now = DateTime.now().millisecondsSinceEpoch;
    final expiry = SharedPreferencesUtil().tokenExpirationTime;
    final cachedAuthToken = SharedPreferencesUtil().authToken;
    final hasValidCachedToken = cachedAuthToken.isNotEmpty &&
        _isValidSupabaseAccessToken(cachedAuthToken) &&
        expiry > now + const Duration(minutes: 5).inMilliseconds;
    if (hasValidCachedToken) {
      return cachedAuthToken;
    }

    if (cachedAuthToken.isNotEmpty && !_isValidSupabaseAccessToken(cachedAuthToken)) {
      Logger.debug('Discarding cached auth token because it is not a Supabase session token');
      SharedPreferencesUtil().authToken = '';
      SharedPreferencesUtil().tokenExpirationTime = 0;
    }

    final refreshToken = SharedPreferencesUtil().refreshToken;
    if (refreshToken.isEmpty) {
      _clearCachedAuth();
      return null;
    }

    try {
      final response = await http.post(
        Uri.parse('${Env.apiBaseUrl}v1/auth/refresh'),
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: {'refresh_token': refreshToken},
      );

      if (response.statusCode != 200) {
        Logger.debug('Supabase refresh failed: ${response.statusCode} ${response.body}');
        _clearCachedAuth();
        _authStateController.add(null);
        return null;
      }

      final payload = json.decode(response.body) as Map<String, dynamic>;
      final user = _extractUser(payload, null);
      _storeSession(payload, user);
      _authStateController.add(user);
      return SharedPreferencesUtil().authToken;
    } catch (e) {
      Logger.debug('Supabase refresh error: $e');
      _clearCachedAuth();
      _authStateController.add(null);
      return null;
    }
  }

  Future<AuthResult?> authenticateWithProvider(String provider) async {
    try {
      final state = _generateState();
      const redirectUri = 'omi://auth/callback';

      final authUrl = '${Env.apiBaseUrl}v1/auth/authorize'
          '?provider=$provider'
          '&redirect_uri=${Uri.encodeComponent(redirectUri)}'
          '&state=$state';

      final appLinks = AppLinks();
      late StreamSubscription linkSubscription;
      final completer = Completer<String>();

      linkSubscription = appLinks.uriLinkStream.listen(
        (Uri uri) {
          if (uri.scheme == 'omi' && uri.host == 'auth' && uri.path == '/callback' && !completer.isCompleted) {
            linkSubscription.cancel();
            _deepLinkChannel.setMethodCallHandler(null);
            completer.complete(uri.toString());
          }
        },
        onError: (error) {
          if (!completer.isCompleted) {
            linkSubscription.cancel();
            _deepLinkChannel.setMethodCallHandler(null);
            completer.completeError(error);
          }
        },
      );

      _deepLinkChannel.setMethodCallHandler((call) async {
        if (call.method != 'onDeepLink') return;
        final urlString = call.arguments as String;
        final uri = Uri.parse(urlString);
        if (uri.scheme == 'omi' && uri.host == 'auth' && uri.path == '/callback' && !completer.isCompleted) {
          linkSubscription.cancel();
          _deepLinkChannel.setMethodCallHandler(null);
          completer.complete(urlString);
        }
      });

      final launched = await launchUrl(Uri.parse(authUrl), mode: LaunchMode.inAppBrowserView);
      if (!launched) {
        linkSubscription.cancel();
        _deepLinkChannel.setMethodCallHandler(null);
        throw Exception('Failed to launch authentication URL');
      }

      final result = await completer.future.timeout(
        const Duration(minutes: 5),
        onTimeout: () {
          linkSubscription.cancel();
          _deepLinkChannel.setMethodCallHandler(null);
          throw Exception('Authentication timeout');
        },
      );

      final uri = Uri.parse(result);
      final code = uri.queryParameters['code'];
      final returnedState = uri.queryParameters['state'];

      if (code == null) {
        throw Exception('No authorization code received');
      }
      if (returnedState != state) {
        throw Exception('Invalid state parameter');
      }

      final session = await _exchangeCodeForSession(code, redirectUri);
      if (session == null) {
        throw Exception('Failed to exchange code for Supabase session');
      }

      final user = _extractUser(session, provider);
      _storeSession(session, user);
      if (!_isValidSupabaseAccessToken(SharedPreferencesUtil().authToken)) {
        final hydratedToken = await getIdToken();
        if (hydratedToken == null) {
          throw Exception('Failed to hydrate Supabase access token after OAuth exchange');
        }
      }
      _authStateController.add(user);

      await _restoreOnboardingState();

      return AuthResult(
        user: user,
        accessToken: SharedPreferencesUtil().authToken,
        refreshToken: SharedPreferencesUtil().refreshToken,
        expiresIn: _coerceInt(session['expires_in'], fallback: 3600),
        provider: provider,
      );
    } catch (e) {
      Logger.debug('OAuth authentication error: $e');
      Logger.handle(e, StackTrace.current, message: 'Authentication failed');
      return null;
    }
  }

  Future<Map<String, dynamic>?> _exchangeCodeForSession(String code, String redirectUri) async {
    try {
      final response = await http.post(
        Uri.parse('${Env.apiBaseUrl}v1/auth/token'),
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: {
          'grant_type': 'authorization_code',
          'code': code,
          'redirect_uri': redirectUri,
        },
      );

      if (response.statusCode == 200) {
        return json.decode(response.body) as Map<String, dynamic>;
      }

      Logger.debug('Token exchange failed: ${response.statusCode} ${response.body}');
      return null;
    } catch (e) {
      Logger.debug('Token exchange error: $e');
      return null;
    }
  }

  Map<String, dynamic> _getSessionEnvelope(Map<String, dynamic> session) {
    final nestedSession = session['session'];
    if (nestedSession is Map<String, dynamic>) {
      return nestedSession;
    }
    return session;
  }

  bool _isSupabaseSessionClaims(Map<String, dynamic>? claims) {
    if (claims == null || claims.isEmpty) return false;

    final issuer = claims['iss']?.toString() ?? '';
    final role = claims['role']?.toString() ?? '';
    final audience = claims['aud'];

    if (role == 'authenticated' || role == 'service_role') {
      return true;
    }
    if (issuer.contains('/auth/v1')) {
      return true;
    }
    if (audience == 'authenticated') {
      return true;
    }
    if (audience is List && audience.contains('authenticated')) {
      return true;
    }
    return false;
  }

  bool _isValidSupabaseAccessToken(String? token) {
    if (token == null || token.isEmpty) return false;
    return _isSupabaseSessionClaims(_decodeJwtClaims(token));
  }

  bool _looksLikeJwt(String? token) {
    if (token == null || token.isEmpty) return false;
    final parts = token.split('.');
    return parts.length == 3 && parts.every((part) => part.isNotEmpty);
  }

  AppAuthUser _extractUser(Map<String, dynamic> session, String? provider) {
    final sessionEnvelope = _getSessionEnvelope(session);
    final userJson = session['user'] is Map<String, dynamic>
        ? session['user'] as Map<String, dynamic>
        : sessionEnvelope['user'] is Map<String, dynamic>
            ? sessionEnvelope['user'] as Map<String, dynamic>
            : {};
    final userMetadata = userJson['user_metadata'] is Map<String, dynamic>
        ? userJson['user_metadata'] as Map<String, dynamic>
        : <String, dynamic>{};

    final accessClaims = _decodeJwtClaims(
      (session['access_token'] ?? sessionEnvelope['access_token']) as String?,
    );
    final providerClaims = _decodeJwtClaims(session['id_token'] as String?);

    final uid = (userJson['id'] ?? userJson['sub'] ?? accessClaims?['sub'] ?? '').toString();
    final email = (userJson['email'] ?? accessClaims?['email'] ?? providerClaims?['email'])?.toString();

    String? givenName = userMetadata['given_name']?.toString();
    String? familyName = userMetadata['family_name']?.toString();
    String? displayName = userMetadata['full_name']?.toString() ?? userMetadata['name']?.toString();

    if ((givenName == null || givenName.isEmpty) && providerClaims != null) {
      givenName = providerClaims['given_name']?.toString();
    }
    if ((familyName == null || familyName.isEmpty) && providerClaims != null) {
      familyName = providerClaims['family_name']?.toString();
    }
    if ((displayName == null || displayName.isEmpty) && providerClaims != null) {
      displayName = providerClaims['name']?.toString();
    }
    if ((displayName == null || displayName.isEmpty) && providerClaims != null) {
      final joined = [givenName, familyName].whereType<String>().where((value) => value.isNotEmpty).join(' ').trim();
      displayName = joined.isEmpty ? null : joined;
    }

    return AppAuthUser(
      uid: uid,
      email: email == null || email.isEmpty ? null : email,
      displayName: displayName == null || displayName.isEmpty ? null : displayName,
      givenName: givenName == null || givenName.isEmpty ? null : givenName,
      familyName: familyName == null || familyName.isEmpty ? null : familyName,
    );
  }

  Map<String, dynamic>? _decodeJwtClaims(String? jwtToken) {
    if (jwtToken == null || jwtToken.isEmpty) return null;
    final parts = jwtToken.split('.');
    if (parts.length < 2) return null;

    var normalized = parts[1].replaceAll('-', '+').replaceAll('_', '/');
    while (normalized.length % 4 != 0) {
      normalized += '=';
    }

    try {
      final decoded = utf8.decode(base64Decode(normalized));
      return json.decode(decoded) as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
  }

  void _storeSession(Map<String, dynamic> session, AppAuthUser user) {
    final sessionEnvelope = _getSessionEnvelope(session);
    final accessToken = (session['access_token'] ?? sessionEnvelope['access_token'] ?? '').toString();
    final refreshToken = (session['refresh_token'] ?? sessionEnvelope['refresh_token'] ?? '').toString();
    final hasUsableAccessToken = _isValidSupabaseAccessToken(accessToken) || _looksLikeJwt(accessToken);

    if (refreshToken.isEmpty) {
      throw Exception('Missing Supabase refresh token');
    }
    if (!hasUsableAccessToken) {
      Logger.debug(
        'Received OAuth session without a directly usable access token; storing refresh token and hydrating via refresh flow',
      );
    }

    SharedPreferencesUtil().uid = user.uid;
    SharedPreferencesUtil().email = user.email ?? '';
    SharedPreferencesUtil().givenName = user.givenName ?? '';
    SharedPreferencesUtil().familyName = user.familyName ?? '';
    SharedPreferencesUtil().authToken = hasUsableAccessToken ? accessToken : '';
    SharedPreferencesUtil().refreshToken = refreshToken;

    final expiresIn = _coerceInt(session['expires_in'] ?? sessionEnvelope['expires_in'], fallback: 3600);
    final expirationTime = DateTime.now().add(Duration(seconds: expiresIn)).millisecondsSinceEpoch;
    SharedPreferencesUtil().tokenExpirationTime = hasUsableAccessToken ? expirationTime : 0;
  }

  int _coerceInt(Object? value, {required int fallback}) {
    if (value is int) return value;
    if (value is String) return int.tryParse(value) ?? fallback;
    return fallback;
  }

  Future<void> restoreOnboardingState() async {
    return _restoreOnboardingState();
  }

  Future<void> _restoreOnboardingState() async {
    try {
      final state = await getUserOnboardingState();
      if (state != null) {
        if (state['completed'] == true) {
          SharedPreferencesUtil().onboardingCompleted = true;
        }
        final acquisitionSource = state['acquisition_source'] as String? ?? '';
        if (acquisitionSource.isNotEmpty) {
          SharedPreferencesUtil().foundOmiSource = acquisitionSource;
        }
        final serverLanguage = await getUserPrimaryLanguage();
        if (serverLanguage != null && serverLanguage.isNotEmpty) {
          SharedPreferencesUtil().userPrimaryLanguage = serverLanguage;
          SharedPreferencesUtil().hasSetPrimaryLanguage = true;
        }
      }
    } catch (e) {
      Logger.debug('restoreOnboardingState error: $e');
    }
  }

  Future<void> updateGivenName(String fullName) async {
    final trimmed = fullName.trim();
    if (trimmed.isEmpty) return;
    final parts = trimmed.split(' ');
    SharedPreferencesUtil().givenName = parts.first;
    SharedPreferencesUtil().familyName = parts.length > 1 ? parts.sublist(1).join(' ') : '';
    _authStateController.add(getCurrentUser());
  }

  String _generateState() {
    final random = Random.secure();
    final bytes = List<int>.generate(32, (_) => random.nextInt(256));
    return base64Url.encode(bytes);
  }

  Future<AuthResult?> linkWithProvider(String provider) async {
    return authenticateWithProvider(provider);
  }

  Future<AuthResult?> linkWithGoogle() async {
    return linkWithProvider('google');
  }

  Future<AuthResult?> linkWithApple() async {
    return linkWithProvider('apple');
  }
}
