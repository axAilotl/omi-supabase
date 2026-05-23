import 'dart:async';

import 'package:omi/services/audio_sources/audio_source.dart';
import 'package:omi/services/background_capture/background_capture_models.dart';
import 'package:omi/services/background_capture/background_capture_pipeline.dart';

typedef BackgroundCaptureAudioListener = Future<StreamSubscription?> Function(
    String deviceId, void Function(List<int> bytes) onAudioBytesReceived);

typedef BackgroundCaptureSocketConnector = Future<BackgroundCaptureTranscriptionSink?> Function(
    BackgroundCaptureSocketConfig socket);

class BackgroundCaptureStartRequest {
  final BackgroundCaptureSessionConfig config;
  final BackgroundCaptureAudioListener listenToAudio;
  final AudioSource? audioSource;
  final BackgroundCaptureTranscriptionSink? transcriptionSink;
  final BackgroundCaptureSocketConnector? connectSocket;
  final BackgroundCaptureWalHooks? walHooks;
  final BackgroundCaptureWalPolicy walCapturePolicy;

  const BackgroundCaptureStartRequest({
    required this.config,
    required this.listenToAudio,
    this.audioSource,
    this.transcriptionSink,
    this.connectSocket,
    this.walHooks,
    this.walCapturePolicy = BackgroundCaptureWalPolicies.disabled,
  });
}

abstract class IBackgroundCaptureService {
  BackgroundCaptureRuntimeStatus get status;

  Stream<BackgroundCaptureRuntimeStatusEvent> get events;

  Future<void> start(BackgroundCaptureStartRequest request);

  Future<void> stop();

  Future<BackgroundCapturePacketResult> handleAudioBytes(List<int> bytes);
}

class BackgroundCaptureService implements IBackgroundCaptureService {
  final StreamController<BackgroundCaptureRuntimeStatusEvent> _events = StreamController.broadcast();

  BackgroundCaptureRuntimeStatus _status = BackgroundCaptureRuntimeStatus.idle;
  BackgroundCaptureSession? _session;
  StreamSubscription? _audioSubscription;

  @override
  BackgroundCaptureRuntimeStatus get status => _status;

  @override
  Stream<BackgroundCaptureRuntimeStatusEvent> get events => _events.stream;

  @override
  Future<void> start(BackgroundCaptureStartRequest request) async {
    await stop();
    _emit(BackgroundCaptureRuntimeStatus.starting);

    try {
      final source = request.audioSource ??
          createBackgroundCaptureAudioSource(
            deviceType: request.config.deviceType,
            codec: request.config.codec,
            deviceId: request.config.deviceId,
            deviceModel: request.config.deviceModel,
          );

      await request.walHooks?.onAudioCodecChanged?.call(request.config.codec);
      request.walHooks?.setDeviceInfo?.call(request.config.deviceId, request.config.deviceModel);

      final sink = request.transcriptionSink ??
          (request.config.sendToTranscription ? await request.connectSocket?.call(request.config.socket) : null);

      _session = BackgroundCaptureSession(
        config: request.config,
        audioSource: source,
        transcriptionSink: sink,
        walHooks: request.walHooks,
        walCapturePolicy: request.walCapturePolicy,
      );

      _audioSubscription = await request.listenToAudio(request.config.deviceId, (bytes) {
        unawaited(_handleBytesFromStream(bytes));
      });
      _emit(BackgroundCaptureRuntimeStatus.running);
    } catch (e) {
      _emit(BackgroundCaptureRuntimeStatus.error, error: e);
      rethrow;
    }
  }

  @override
  Future<void> stop() async {
    if (_status == BackgroundCaptureRuntimeStatus.idle || _status == BackgroundCaptureRuntimeStatus.stopped) {
      return;
    }

    _emit(BackgroundCaptureRuntimeStatus.stopping);
    await _audioSubscription?.cancel();
    _audioSubscription = null;
    _session = null;
    _emit(BackgroundCaptureRuntimeStatus.stopped);
  }

  @override
  Future<BackgroundCapturePacketResult> handleAudioBytes(List<int> bytes) async {
    final session = _session;
    if (session == null) return BackgroundCapturePacketResult.empty;
    return session.processAudioPacket(List<int>.from(bytes));
  }

  Future<void> _handleBytesFromStream(List<int> bytes) async {
    try {
      final session = _session;
      final result = await handleAudioBytes(bytes);
      if (session?.config.sendToTranscription == true &&
          result.rawBytesReceived > 0 &&
          result.socketBytesSent == 0 &&
          !result.socketConnected) {
        _emit(BackgroundCaptureRuntimeStatus.socketDisconnected);
      }
    } catch (e) {
      _emit(BackgroundCaptureRuntimeStatus.error, error: e);
    }
  }

  void _emit(BackgroundCaptureRuntimeStatus status, {String? message, Object? error}) {
    _status = status;
    _events.add(BackgroundCaptureRuntimeStatusEvent(status, message: message, error: error));
  }
}
