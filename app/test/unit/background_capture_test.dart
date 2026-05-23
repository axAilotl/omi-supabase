import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/audio_sources/audio_source.dart';
import 'package:omi/services/audio_sources/ble_device_source.dart';
import 'package:omi/services/audio_sources/friend_pendant_source.dart';
import 'package:omi/services/background_capture/background_capture.dart';

class _WalRecorder {
  final captured = <WalFrame>[];
  final synced = <FrameSyncKey>[];
  BleAudioCodec? codec;
  String? deviceId;
  String? deviceModel;

  BackgroundCaptureWalHooks hooks() {
    return BackgroundCaptureWalHooks(
      onAudioCodecChanged: (value) {
        codec = value;
      },
      setDeviceInfo: (id, model) {
        deviceId = id;
        deviceModel = model;
      },
      onFrameCaptured: captured.add,
      markFrameSynced: synced.add,
    );
  }
}

BackgroundCaptureSessionConfig _config({required DeviceType deviceType, required BleAudioCodec codec}) {
  return BackgroundCaptureSessionConfig(
    deviceId: 'device-1',
    deviceType: deviceType,
    codec: codec,
    deviceModel: deviceType == DeviceType.friendPendant ? 'Friend Pendant' : 'Omi',
    socket: BackgroundCaptureSocketConfig(codec: codec, source: 'test'),
  );
}

void main() {
  group('FriendPendantSource', () {
    test('passes through native 30-byte LC3 frames', () {
      final source = FriendPendantSource(deviceId: 'friend-1');
      final frame = List<int>.generate(FriendPendantSource.lc3FrameSize, (index) => index);

      final frames = source.processBytes(frame);

      expect(frames, hasLength(1));
      expect(frames.first.payload, frame);
      expect(frames.first.syncKey.bytes, [0]);
      expect(source.getSocketPayloads(frame), [frame]);
    });

    test('splits raw 95-byte BLE packets into three LC3 frames and strips footer', () {
      final source = FriendPendantSource(deviceId: 'friend-1');
      final packet = [
        ...List<int>.generate(FriendPendantSource.lc3DataSize, (index) => index),
        0xF0,
        0xF1,
        0xF2,
        0xF3,
        0xF4,
      ];

      final frames = source.processBytes(packet);

      expect(frames, hasLength(3));
      expect(frames[0].payload, List<int>.generate(30, (index) => index));
      expect(frames[1].payload, List<int>.generate(30, (index) => index + 30));
      expect(frames[2].payload, List<int>.generate(30, (index) => index + 60));
      expect(frames.map((frame) => frame.syncKey.bytes.single), [0, 1, 2]);
      expect(source.getSocketPayload(packet), hasLength(FriendPendantSource.lc3DataSize));
    });
  });

  group('BackgroundCaptureSession', () {
    test('streams Omi/OpenGlass payloads and marks WAL frames synced after send', () async {
      final sent = <List<int>>[];
      final wal = _WalRecorder();
      final source = BleDeviceSource(codec: BleAudioCodec.opus, deviceId: 'omi-1', deviceModel: 'Omi');
      final session = BackgroundCaptureSession(
        config: _config(deviceType: DeviceType.omi, codec: BleAudioCodec.opus),
        audioSource: source,
        transcriptionSink: BackgroundCaptureTranscriptionSink(
          isConnected: () => true,
          send: (payload) {
            sent.add(List<int>.from(payload));
          },
        ),
        walHooks: wal.hooks(),
        walCapturePolicy: (context) => BackgroundCaptureWalPolicies.omiOpenGlassOpusWhenOfflineOrLocalStorage(
          context,
          unlimitedLocalStorageEnabled: true,
        ),
      );

      final rawPacket = [0x05, 0x00, 0x02, ...List<int>.filled(80, 0xAA)];
      final result = await session.processAudioPacket(rawPacket);

      expect(sent, [List<int>.filled(80, 0xAA)]);
      expect(wal.captured, hasLength(1));
      expect(wal.captured.first.payload, List<int>.filled(80, 0xAA));
      expect(wal.synced, [
        FrameSyncKey([0x05, 0x00, 0x02]),
      ]);
      expect(result.socketBytesSent, 80);
      expect(result.walFramesCaptured, 1);
      expect(result.walFramesMarkedSynced, 1);
    });

    test('captures Omi/OpenGlass WAL frames without marking synced when socket is down', () async {
      final sent = <List<int>>[];
      final wal = _WalRecorder();
      final session = BackgroundCaptureSession(
        config: _config(deviceType: DeviceType.openglass, codec: BleAudioCodec.opus),
        audioSource: BleDeviceSource(codec: BleAudioCodec.opus, deviceId: 'glass-1', deviceModel: 'OpenGlass'),
        transcriptionSink: BackgroundCaptureTranscriptionSink(isConnected: () => false, send: sent.add),
        walHooks: wal.hooks(),
        walCapturePolicy: (context) => BackgroundCaptureWalPolicies.omiOpenGlassOpusWhenOfflineOrLocalStorage(
          context,
          unlimitedLocalStorageEnabled: false,
        ),
      );

      final result = await session.processAudioPacket([0x01, 0x02, 0x03, ...List<int>.filled(80, 0xBB)]);

      expect(sent, isEmpty);
      expect(wal.captured, hasLength(1));
      expect(wal.synced, isEmpty);
      expect(result.walEnabled, isTrue);
      expect(result.socketBytesSent, 0);
    });

    test('normalizes Friend Pendant raw LC3 packets for socket and WAL hooks', () async {
      final sent = <List<int>>[];
      final wal = _WalRecorder();
      final session = BackgroundCaptureSession(
        config: _config(deviceType: DeviceType.friendPendant, codec: BleAudioCodec.lc3FS1030),
        audioSource: FriendPendantSource(deviceId: 'friend-1'),
        transcriptionSink: BackgroundCaptureTranscriptionSink(
          isConnected: () => true,
          send: (payload) {
            sent.add(List<int>.from(payload));
          },
        ),
        walHooks: wal.hooks(),
        walCapturePolicy: BackgroundCaptureWalPolicies.always,
      );
      final packet = [...List<int>.generate(FriendPendantSource.lc3DataSize, (index) => index), 0, 0, 0, 0, 0];

      final result = await session.processAudioPacket(packet);

      expect(sent, hasLength(3));
      expect(sent.every((payload) => payload.length == FriendPendantSource.lc3FrameSize), isTrue);
      expect(wal.captured, hasLength(3));
      expect(wal.synced, hasLength(3));
      expect(result.socketBytesSent, FriendPendantSource.lc3DataSize);
    });
  });

  group('BackgroundCaptureService', () {
    test('starts a facade session, configures WAL hooks, and handles packets', () async {
      final controller = StreamController<List<int>>();
      final sent = <List<int>>[];
      final wal = _WalRecorder();
      final service = BackgroundCaptureService();

      await service.start(
        BackgroundCaptureStartRequest(
          config: _config(deviceType: DeviceType.omi, codec: BleAudioCodec.opus),
          audioSource: BleDeviceSource(codec: BleAudioCodec.opus, deviceId: 'omi-1', deviceModel: 'Omi'),
          transcriptionSink: BackgroundCaptureTranscriptionSink(
            isConnected: () => true,
            send: (payload) {
              sent.add(List<int>.from(payload));
            },
          ),
          walHooks: wal.hooks(),
          walCapturePolicy: BackgroundCaptureWalPolicies.always,
          listenToAudio: (deviceId, onAudioBytesReceived) async {
            return controller.stream.listen(onAudioBytesReceived);
          },
        ),
      );

      final result = await service.handleAudioBytes([0x01, 0x02, 0x03, ...List<int>.filled(80, 0xCC)]);

      expect(service.status, BackgroundCaptureRuntimeStatus.running);
      expect(wal.codec, BleAudioCodec.opus);
      expect(wal.deviceId, 'device-1');
      expect(wal.deviceModel, 'Omi');
      expect(result.socketBytesSent, 80);
      expect(sent, hasLength(1));

      await service.stop();
      await controller.close();
      expect(service.status, BackgroundCaptureRuntimeStatus.stopped);
    });
  });
}
