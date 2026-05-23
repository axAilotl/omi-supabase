import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/audio_sources/audio_source.dart';

/// Audio source for Friend Pendant LC3 audio.
///
/// Native device code currently emits individual 30-byte LC3 frames, while a
/// lower-level background runtime may receive the 95-byte BLE packet shape:
/// 90 bytes of LC3 audio followed by a 5-byte footer. This source accepts both
/// shapes and normalizes them into 30-byte frames.
class FriendPendantSource implements AudioSource {
  static const int packetFooterSize = 5;
  static const int packetSize = 95;
  static const int lc3DataSize = 90;
  static const int lc3FrameSize = 30;

  @override
  BleAudioCodec get codec => BleAudioCodec.lc3FS1030;

  @override
  final String deviceId;

  @override
  final String deviceModel;

  int _frameIndex = 0;

  FriendPendantSource({required this.deviceId, this.deviceModel = 'Friend Pendant'});

  @override
  List<WalFrame> processBytes(List<int> rawBytes) {
    return getSocketPayloads(
      rawBytes,
    ).map((payload) => WalFrame(payload: payload, syncKey: FrameSyncKey.fromIndex(_frameIndex++))).toList();
  }

  @override
  List<int> getSocketPayload(List<int> rawBytes) {
    final payloads = getSocketPayloads(rawBytes);
    if (payloads.isEmpty) return const [];
    if (payloads.length == 1) return payloads.first;
    return payloads.expand((payload) => payload).toList();
  }

  /// Returns socket-ready LC3 frame payloads.
  ///
  /// The foreground Flutter connection already splits packets into 30-byte
  /// frames. The future Android headless path can pass raw 95-byte packets and
  /// get the same frame stream.
  List<List<int>> getSocketPayloads(List<int> rawBytes) {
    if (rawBytes.isEmpty) return const [];

    final audioBytes = _stripFooterIfPresent(rawBytes);
    if (audioBytes.length % lc3FrameSize != 0) return const [];

    final frames = <List<int>>[];
    for (int offset = 0; offset < audioBytes.length; offset += lc3FrameSize) {
      frames.add(audioBytes.sublist(offset, offset + lc3FrameSize));
    }
    return frames;
  }

  List<int> _stripFooterIfPresent(List<int> rawBytes) {
    if (rawBytes.length == packetSize) {
      return rawBytes.sublist(0, lc3DataSize);
    }

    final payloadLength = rawBytes.length - packetFooterSize;
    if (payloadLength > 0 && payloadLength % lc3FrameSize == 0) {
      return rawBytes.sublist(0, payloadLength);
    }

    return rawBytes;
  }

  @override
  List<WalFrame> flush() => [];
}
