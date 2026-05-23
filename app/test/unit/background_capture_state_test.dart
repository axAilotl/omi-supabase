import 'package:flutter_test/flutter_test.dart';

import 'package:omi/services/background_capture/background_capture_state.dart';

void main() {
  group('BackgroundCaptureState', () {
    final updatedAt = DateTime.utc(2026, 5, 22, 12, 30);

    test('round-trips every status through json', () {
      for (final status in BackgroundCaptureStatus.values) {
        final state = BackgroundCaptureState(
          status: status,
          deviceId: 'AA:BB:CC:DD:EE:FF',
          deviceName: 'Omi',
          conversationId: 'conversation-1',
          bufferedAudioBytes: 640,
          errorCode: status == BackgroundCaptureStatus.error ? 'socket_closed' : null,
          errorMessage: status == BackgroundCaptureStatus.error ? 'Socket closed' : null,
          updatedAt: updatedAt,
        );

        final decoded = BackgroundCaptureState.fromJson(state.toJson());

        expect(decoded.schemaVersion, BackgroundCaptureState.currentSchemaVersion);
        expect(decoded.status, status);
        expect(decoded.deviceId, 'AA:BB:CC:DD:EE:FF');
        expect(decoded.deviceName, 'Omi');
        expect(decoded.conversationId, 'conversation-1');
        expect(decoded.bufferedAudioBytes, 640);
        expect(decoded.updatedAt, updatedAt);
      }
    });

    test('maps unknown native status to error without throwing', () {
      final state = BackgroundCaptureState.fromJson({
        'status': 'nativeAddedNewState',
        'updated_at': updatedAt.toIso8601String(),
      });

      expect(state.status, BackgroundCaptureStatus.error);
      expect(state.errorCode, 'unknown_status');
      expect(state.errorMessage, contains('nativeAddedNewState'));
    });

    test('derives active, capture, and foreground notification flags', () {
      expect(BackgroundCaptureState(status: BackgroundCaptureStatus.starting, updatedAt: updatedAt).isActive, true);
      expect(
        BackgroundCaptureState(status: BackgroundCaptureStatus.recording, updatedAt: updatedAt).isCapturingAudio,
        true,
      );
      expect(
        BackgroundCaptureState(
          status: BackgroundCaptureStatus.buffering,
          updatedAt: updatedAt,
        ).shouldShowForegroundNotification,
        true,
      );
      expect(
        BackgroundCaptureState(
          status: BackgroundCaptureStatus.error,
          updatedAt: updatedAt,
        ).shouldShowForegroundNotification,
        true,
      );
      expect(BackgroundCaptureState.stopped(updatedAt: updatedAt).shouldShowForegroundNotification, false);
      expect(
        BackgroundCaptureState(
          status: BackgroundCaptureStatus.userStopped,
          updatedAt: updatedAt,
        ).wasExplicitlyStoppedByUser,
        true,
      );
    });

    test('copyWith can clear optional groups', () {
      final state = BackgroundCaptureState(
        status: BackgroundCaptureStatus.error,
        deviceId: 'device-1',
        deviceName: 'Omi',
        conversationId: 'conversation-1',
        errorCode: 'network',
        errorMessage: 'offline',
        updatedAt: updatedAt,
      );

      final copied = state.copyWith(
        status: BackgroundCaptureStatus.starting,
        clearConversation: true,
        clearError: true,
      );

      expect(copied.status, BackgroundCaptureStatus.starting);
      expect(copied.deviceId, 'device-1');
      expect(copied.deviceName, 'Omi');
      expect(copied.conversationId, isNull);
      expect(copied.errorCode, isNull);
      expect(copied.errorMessage, isNull);
    });
  });
}
