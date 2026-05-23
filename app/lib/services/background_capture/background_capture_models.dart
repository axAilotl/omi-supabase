import 'dart:async';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/models/custom_stt_config.dart';
import 'package:omi/services/audio_sources/audio_source.dart';

enum BackgroundCaptureRuntimeStatus { idle, starting, running, socketDisconnected, stopping, stopped, error }

class BackgroundCaptureRuntimeStatusEvent {
  final BackgroundCaptureRuntimeStatus status;
  final DateTime at;
  final String? message;
  final Object? error;

  BackgroundCaptureRuntimeStatusEvent(this.status, {DateTime? at, this.message, this.error})
      : at = at ?? DateTime.now();
}

class BackgroundCaptureReconnectPolicy {
  final bool enabled;
  final Duration initialDelay;
  final Duration maxDelay;
  final int maxAttempts;

  const BackgroundCaptureReconnectPolicy({
    this.enabled = true,
    this.initialDelay = const Duration(seconds: 1),
    this.maxDelay = const Duration(seconds: 30),
    this.maxAttempts = 0,
  });

  const BackgroundCaptureReconnectPolicy.disabled()
      : enabled = false,
        initialDelay = Duration.zero,
        maxDelay = Duration.zero,
        maxAttempts = 0;
}

class BackgroundCaptureSocketConfig {
  final BleAudioCodec codec;
  final int sampleRate;
  final int channels;
  final bool isPcm;
  final String language;
  final String? source;
  final bool force;
  final CustomSttConfig? customSttConfig;

  BackgroundCaptureSocketConfig({
    required this.codec,
    int? sampleRate,
    int? channels,
    bool? isPcm,
    this.language = 'multi',
    this.source,
    this.force = false,
    this.customSttConfig,
  })  : sampleRate = sampleRate ?? mapCodecToSampleRate(codec),
        channels = channels ?? ((codec == BleAudioCodec.pcm16 || codec == BleAudioCodec.pcm8) ? 1 : 2),
        isPcm = isPcm ?? (codec == BleAudioCodec.pcm16 || codec == BleAudioCodec.pcm8);
}

class BackgroundCaptureSessionConfig {
  final String deviceId;
  final DeviceType deviceType;
  final BleAudioCodec codec;
  final String deviceModel;
  final BackgroundCaptureSocketConfig socket;
  final bool sendToTranscription;
  final BackgroundCaptureReconnectPolicy reconnectPolicy;

  const BackgroundCaptureSessionConfig({
    required this.deviceId,
    required this.deviceType,
    required this.codec,
    required this.deviceModel,
    required this.socket,
    this.sendToTranscription = true,
    this.reconnectPolicy = const BackgroundCaptureReconnectPolicy(),
  });
}

class BackgroundCaptureWalContext {
  final String deviceId;
  final DeviceType deviceType;
  final BleAudioCodec codec;
  final bool socketConnected;
  final bool sendToTranscription;

  const BackgroundCaptureWalContext({
    required this.deviceId,
    required this.deviceType,
    required this.codec,
    required this.socketConnected,
    required this.sendToTranscription,
  });
}

typedef BackgroundCaptureWalPolicy = bool Function(BackgroundCaptureWalContext context);

class BackgroundCaptureWalPolicies {
  const BackgroundCaptureWalPolicies._();

  static bool disabled(BackgroundCaptureWalContext context) => false;

  static bool always(BackgroundCaptureWalContext context) => true;

  static bool omiOpenGlassOpusWhenOfflineOrLocalStorage(
    BackgroundCaptureWalContext context, {
    required bool unlimitedLocalStorageEnabled,
  }) {
    final supportedDevice = context.deviceType == DeviceType.omi || context.deviceType == DeviceType.openglass;
    return supportedDevice &&
        context.codec.isOpusSupported() &&
        (!context.socketConnected || unlimitedLocalStorageEnabled);
  }
}

class BackgroundCaptureWalHooks {
  final FutureOr<void> Function(BleAudioCodec codec)? onAudioCodecChanged;
  final void Function(String? deviceId, String? deviceModel)? setDeviceInfo;
  final void Function(WalFrame frame) onFrameCaptured;
  final void Function(FrameSyncKey syncKey) markFrameSynced;

  const BackgroundCaptureWalHooks({
    this.onAudioCodecChanged,
    this.setDeviceInfo,
    required this.onFrameCaptured,
    required this.markFrameSynced,
  });
}

class BackgroundCaptureTranscriptionSink {
  final bool Function() isConnected;
  final FutureOr<void> Function(List<int> payload) send;

  const BackgroundCaptureTranscriptionSink({required this.isConnected, required this.send});
}

class BackgroundCapturePacketResult {
  final int rawBytesReceived;
  final int socketBytesSent;
  final int walFramesCaptured;
  final int walFramesMarkedSynced;
  final bool socketConnected;
  final bool walEnabled;
  final List<List<int>> socketPayloads;
  final List<WalFrame> walFrames;

  const BackgroundCapturePacketResult({
    required this.rawBytesReceived,
    required this.socketBytesSent,
    required this.walFramesCaptured,
    required this.walFramesMarkedSynced,
    required this.socketConnected,
    required this.walEnabled,
    required this.socketPayloads,
    required this.walFrames,
  });

  static const empty = BackgroundCapturePacketResult(
    rawBytesReceived: 0,
    socketBytesSent: 0,
    walFramesCaptured: 0,
    walFramesMarkedSynced: 0,
    socketConnected: false,
    walEnabled: false,
    socketPayloads: [],
    walFrames: [],
  );
}
