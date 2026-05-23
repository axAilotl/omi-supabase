import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/audio_sources/audio_source.dart';
import 'package:omi/services/audio_sources/ble_device_source.dart';
import 'package:omi/services/audio_sources/friend_pendant_source.dart';
import 'package:omi/services/background_capture/background_capture_models.dart';

AudioSource? createBackgroundCaptureAudioSource({
  required DeviceType deviceType,
  required BleAudioCodec codec,
  required String deviceId,
  required String deviceModel,
}) {
  switch (deviceType) {
    case DeviceType.omi:
    case DeviceType.openglass:
      return BleDeviceSource(codec: codec, deviceId: deviceId, deviceModel: deviceModel);
    case DeviceType.friendPendant:
      return FriendPendantSource(deviceId: deviceId, deviceModel: deviceModel);
    case DeviceType.frame:
    case DeviceType.appleWatch:
    case DeviceType.plaud:
    case DeviceType.bee:
    case DeviceType.fieldy:
    case DeviceType.limitless:
      return null;
  }
}

class BackgroundCaptureSession {
  final BackgroundCaptureSessionConfig config;
  final AudioSource? audioSource;
  final BackgroundCaptureTranscriptionSink? transcriptionSink;
  final BackgroundCaptureWalHooks? walHooks;
  final BackgroundCaptureWalPolicy walCapturePolicy;

  BackgroundCaptureSession({
    required this.config,
    this.audioSource,
    this.transcriptionSink,
    this.walHooks,
    this.walCapturePolicy = BackgroundCaptureWalPolicies.disabled,
  });

  Future<BackgroundCapturePacketResult> processAudioPacket(List<int> rawBytes) async {
    if (rawBytes.isEmpty) return BackgroundCapturePacketResult.empty;

    final socketConnected = config.sendToTranscription && (transcriptionSink?.isConnected() ?? false);
    final walEnabled = walHooks != null &&
        walCapturePolicy(
          BackgroundCaptureWalContext(
            deviceId: config.deviceId,
            deviceType: config.deviceType,
            codec: config.codec,
            socketConnected: socketConnected,
            sendToTranscription: config.sendToTranscription,
          ),
        );

    final frames = audioSource?.processBytes(rawBytes) ?? const <WalFrame>[];
    final socketPayloads = _socketPayloads(rawBytes);

    if (walEnabled) {
      for (final frame in frames) {
        walHooks!.onFrameCaptured(frame);
      }
    }

    int socketBytesSent = 0;
    if (socketConnected) {
      for (final payload in socketPayloads) {
        if (payload.isEmpty) continue;
        await transcriptionSink!.send(payload);
        socketBytesSent += payload.length;
      }

      if (walEnabled) {
        for (final frame in frames) {
          walHooks!.markFrameSynced(frame.syncKey);
        }
      }
    }

    return BackgroundCapturePacketResult(
      rawBytesReceived: rawBytes.length,
      socketBytesSent: socketBytesSent,
      walFramesCaptured: walEnabled ? frames.length : 0,
      walFramesMarkedSynced: walEnabled && socketConnected ? frames.length : 0,
      socketConnected: socketConnected,
      walEnabled: walEnabled,
      socketPayloads: socketPayloads,
      walFrames: frames,
    );
  }

  List<List<int>> _socketPayloads(List<int> rawBytes) {
    final source = audioSource;
    if (source == null) return [rawBytes];
    if (source is FriendPendantSource) {
      return source.getSocketPayloads(rawBytes);
    }

    final payload = source.getSocketPayload(rawBytes);
    if (payload.isEmpty) return const [];
    return [payload];
  }
}
