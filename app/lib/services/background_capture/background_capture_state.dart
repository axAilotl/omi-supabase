/// UI-facing contract for Android companion-device background capture.
///
/// Native and runtime workers can send this payload across a method/event
/// channel without coupling the Flutter UI to service internals. Keep this file
/// pure Dart so it is cheap to test and safe to import from providers/widgets.
enum BackgroundCaptureStatus {
  stopped,
  starting,
  connecting,
  connected,
  recording,
  paused,
  buffering,
  reconnecting,
  error,
  userStopped;

  static BackgroundCaptureStatus? tryParse(Object? value) {
    if (value == null) return null;
    return BackgroundCaptureStatus.values.asNameMap()[value.toString()];
  }
}

class BackgroundCaptureState {
  static const int currentSchemaVersion = 1;

  final int schemaVersion;
  final BackgroundCaptureStatus status;
  final String? deviceId;
  final String? deviceName;
  final String? conversationId;
  final int bufferedAudioBytes;
  final String? errorCode;
  final String? errorMessage;
  final DateTime updatedAt;

  BackgroundCaptureState({
    this.schemaVersion = currentSchemaVersion,
    required this.status,
    this.deviceId,
    this.deviceName,
    this.conversationId,
    this.bufferedAudioBytes = 0,
    this.errorCode,
    this.errorMessage,
    DateTime? updatedAt,
  }) : updatedAt = (updatedAt ?? DateTime.now()).toUtc();

  factory BackgroundCaptureState.stopped({DateTime? updatedAt}) {
    return BackgroundCaptureState(status: BackgroundCaptureStatus.stopped, updatedAt: updatedAt);
  }

  factory BackgroundCaptureState.fromJson(Map<String, dynamic> json) {
    final rawStatus = json['status'];
    final parsedStatus = BackgroundCaptureStatus.tryParse(rawStatus);
    final unknownStatus = rawStatus != null && parsedStatus == null;

    return BackgroundCaptureState(
      schemaVersion: _parseInt(json['schema_version'], fallback: currentSchemaVersion),
      status: parsedStatus ?? BackgroundCaptureStatus.error,
      deviceId: _parseString(json['device_id']),
      deviceName: _parseString(json['device_name']),
      conversationId: _parseString(json['conversation_id']),
      bufferedAudioBytes: _parseInt(json['buffered_audio_bytes']),
      errorCode: _parseString(json['error_code']) ?? (unknownStatus ? 'unknown_status' : null),
      errorMessage: _parseString(json['error_message']) ??
          (unknownStatus ? 'Unknown background capture status: $rawStatus' : null),
      updatedAt: _parseUpdatedAt(json['updated_at']),
    );
  }

  bool get isActive {
    switch (status) {
      case BackgroundCaptureStatus.starting:
      case BackgroundCaptureStatus.connecting:
      case BackgroundCaptureStatus.connected:
      case BackgroundCaptureStatus.recording:
      case BackgroundCaptureStatus.paused:
      case BackgroundCaptureStatus.buffering:
      case BackgroundCaptureStatus.reconnecting:
        return true;
      case BackgroundCaptureStatus.stopped:
      case BackgroundCaptureStatus.error:
      case BackgroundCaptureStatus.userStopped:
        return false;
    }
  }

  bool get isCapturingAudio {
    return status == BackgroundCaptureStatus.recording || status == BackgroundCaptureStatus.buffering;
  }

  bool get shouldShowForegroundNotification {
    return isActive || status == BackgroundCaptureStatus.error;
  }

  bool get wasExplicitlyStoppedByUser => status == BackgroundCaptureStatus.userStopped;

  BackgroundCaptureState copyWith({
    int? schemaVersion,
    BackgroundCaptureStatus? status,
    String? deviceId,
    String? deviceName,
    String? conversationId,
    int? bufferedAudioBytes,
    String? errorCode,
    String? errorMessage,
    DateTime? updatedAt,
    bool clearDevice = false,
    bool clearConversation = false,
    bool clearError = false,
  }) {
    return BackgroundCaptureState(
      schemaVersion: schemaVersion ?? this.schemaVersion,
      status: status ?? this.status,
      deviceId: clearDevice ? null : deviceId ?? this.deviceId,
      deviceName: clearDevice ? null : deviceName ?? this.deviceName,
      conversationId: clearConversation ? null : conversationId ?? this.conversationId,
      bufferedAudioBytes: bufferedAudioBytes ?? this.bufferedAudioBytes,
      errorCode: clearError ? null : errorCode ?? this.errorCode,
      errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
      updatedAt: updatedAt ?? this.updatedAt,
    );
  }

  Map<String, dynamic> toJson() {
    final json = <String, dynamic>{
      'schema_version': schemaVersion,
      'status': status.name,
      'buffered_audio_bytes': bufferedAudioBytes,
      'updated_at': updatedAt.toUtc().toIso8601String(),
    };

    _putIfNotNull(json, 'device_id', deviceId);
    _putIfNotNull(json, 'device_name', deviceName);
    _putIfNotNull(json, 'conversation_id', conversationId);
    _putIfNotNull(json, 'error_code', errorCode);
    _putIfNotNull(json, 'error_message', errorMessage);

    return json;
  }

  static String? _parseString(Object? value) {
    if (value == null) return null;
    return value.toString();
  }

  static int _parseInt(Object? value, {int fallback = 0}) {
    if (value is int) return value;
    if (value is num) return value.toInt();
    if (value is String) return int.tryParse(value) ?? fallback;
    return fallback;
  }

  static DateTime _parseUpdatedAt(Object? value) {
    if (value is DateTime) return value.toUtc();
    if (value is int) return DateTime.fromMillisecondsSinceEpoch(value, isUtc: true);
    if (value is num) return DateTime.fromMillisecondsSinceEpoch(value.toInt(), isUtc: true);
    if (value is String) {
      final parsed = DateTime.tryParse(value);
      if (parsed != null) return parsed.toUtc();
    }
    return DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
  }

  static void _putIfNotNull(Map<String, dynamic> json, String key, Object? value) {
    if (value != null) json[key] = value;
  }
}
