import 'dart:async';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/models/sync_state.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/services/wals/flash_page_wal_sync.dart';
import 'package:omi/services/wals/local_wal_sync.dart';
import 'package:omi/services/wals/sdcard_wal_sync.dart';
import 'package:omi/services/wals/storage_sync.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_interfaces.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';

class WalSyncs implements IWalSync {
  late LocalWalSyncImpl _phoneSync;
  LocalWalSyncImpl get phone => _phoneSync;

  late SDCardWalSyncImpl _sdcardSync;
  SDCardWalSyncImpl get sdcard => _sdcardSync;

  late FlashPageWalSyncImpl _flashPageSync;
  FlashPageWalSyncImpl get flashPage => _flashPageSync;

  late StorageSyncImpl _storageSync;
  StorageSyncImpl get storage => _storageSync;

  final IWalSyncListener listener;

  bool _isCancelled = false;
  SyncLocalFilesResponse? _accumulatedResponse;

  WalSyncs(this.listener) {
    _phoneSync = LocalWalSyncImpl(listener);
    _sdcardSync = SDCardWalSyncImpl(listener);
    _flashPageSync = FlashPageWalSyncImpl(listener);
    _storageSync = StorageSyncImpl(listener);

    _sdcardSync.setLocalSync(_phoneSync);
    _flashPageSync.setLocalSync(_phoneSync);
    _storageSync.setLocalSync(_phoneSync);

    _sdcardSync.loadWifiCredentials();
  }

  @override
  Future deleteWal(Wal wal) async {
    await _phoneSync.deleteWal(wal);
    await _sdcardSync.deleteWal(wal);
    await _flashPageSync.deleteWal(wal);
    await _storageSync.deleteWal(wal);
  }

  @override
  Future<List<Wal>> getMissingWals() async {
    List<Wal> wals = [];
    wals.addAll(await _storageSync.getMissingWals());
    wals.addAll(await _sdcardSync.getMissingWals());
    wals.addAll(await _phoneSync.getMissingWals());
    wals.addAll(await _flashPageSync.getMissingWals());
    return wals;
  }

  Future<List<Wal>> getAllWals() async {
    List<Wal> wals = [];
    wals.addAll(await _storageSync.getMissingWals());
    wals.addAll(await _sdcardSync.getMissingWals());
    wals.addAll(await _phoneSync.getAllWals());
    wals.addAll(await _flashPageSync.getMissingWals());
    return wals;
  }

  Future<WalStats> getWalStats() async {
    final allWals = await getAllWals();
    int phoneFiles = 0;
    int sdcardFiles = 0;
    int fromSdcardFiles = 0;
    int limitlessFiles = 0;
    int fromFlashPageFiles = 0;
    int phoneSize = 0;
    int sdcardSize = 0;
    int syncedFiles = 0;
    int missedFiles = 0;

    for (final wal in allWals) {
      if (wal.storage == WalStorage.sdcard) {
        sdcardFiles++;
        sdcardSize += _estimateWalSize(wal);
      } else if (wal.storage == WalStorage.flashPage) {
        limitlessFiles++;
      } else {
        if (wal.originalStorage == WalStorage.sdcard) {
          fromSdcardFiles++;
        } else if (wal.originalStorage == WalStorage.flashPage) {
          fromFlashPageFiles++;
        } else {
          phoneFiles++;
        }
        phoneSize += _estimateWalSize(wal);
      }

      if (wal.status == WalStatus.synced) {
        syncedFiles++;
      } else if (wal.status == WalStatus.miss) {
        missedFiles++;
      }
    }

    return WalStats(
      totalFiles: allWals.length,
      phoneFiles: phoneFiles,
      sdcardFiles: sdcardFiles,
      fromSdcardFiles: fromSdcardFiles,
      limitlessFiles: limitlessFiles,
      fromFlashPageFiles: fromFlashPageFiles,
      phoneSize: phoneSize,
      sdcardSize: sdcardSize,
      syncedFiles: syncedFiles,
      missedFiles: missedFiles,
    );
  }

  int _estimateWalSize(Wal wal) {
    int bytesPerSecond;
    switch (wal.codec) {
      case BleAudioCodec.opusFS320:
        bytesPerSecond = 16000;
      case BleAudioCodec.opus:
        bytesPerSecond = 8000;
        break;
      case BleAudioCodec.pcm16:
        bytesPerSecond = wal.sampleRate * 2 * wal.channel;
        break;
      case BleAudioCodec.pcm8:
        bytesPerSecond = wal.sampleRate * 1 * wal.channel;
        break;
      default:
        bytesPerSecond = 8000;
    }
    return bytesPerSecond * wal.seconds;
  }

  Future<void> deleteAllSyncedWals() async {
    await _phoneSync.deleteAllSyncedWals();
    await _sdcardSync.deleteAllSyncedWals();
    await _flashPageSync.deleteAllSyncedWals();
    await _storageSync.deleteAllSyncedWals();
  }

  Future<void> deleteAllPendingWals() async {
    await _phoneSync.deleteAllPendingWals();
    await _sdcardSync.deleteAllPendingWals();
    await _flashPageSync.deleteAllPendingWals();
    await _storageSync.deleteAllPendingWals();
  }

  @override
  void start() {
    _phoneSync.start();
    _sdcardSync.start();
    _flashPageSync.start();
    _storageSync.start();
  }

  @override
  Future stop() async {
    await _phoneSync.stop();
    await _sdcardSync.stop();
    await _flashPageSync.stop();
    await _storageSync.stop();
  }

  @override
  Future<SyncLocalFilesResponse?> syncAll({
    IWalSyncProgressListener? progress,
    IWifiConnectionListener? connectionListener,
  }) async {
    _isCancelled = false;
    var resp = SyncLocalFilesResponse(newConversationIds: [], updatedConversationIds: []);
    _accumulatedResponse = resp;

    final allMissing = await getMissingWals();
    DebugLogManager.logEvent('sync_started', {
      'totalMissingWals': allMissing.length,
      'sdcard': allMissing.where((w) => w.storage == WalStorage.sdcard).length,
      'flashPage': allMissing.where((w) => w.storage == WalStorage.flashPage).length,
      'phone': allMissing.where((w) => w.storage == WalStorage.disk || w.storage == WalStorage.mem).length,
    });

    final hasPhoneUploads = allMissing.any((w) => w.storage == WalStorage.disk || w.storage == WalStorage.mem);
    if (hasPhoneUploads) {
      await _uploadPhoneFilesToCloud(
        resp,
        progress,
        logLabel: 'Sync Phase 0: Uploading phone files before device download',
      );
    }

    if (_isCancelled) {
      Logger.debug("WalSyncs: Cancelled after initial phone upload phase");
      DebugLogManager.logWarning('Sync cancelled after initial phone upload phase');
      return resp;
    }

    if (hasPhoneUploads) {
      DebugLogManager.logInfo('Sync deferred device download until phone queue drains', {
        'newConversations': resp.newConversationIds.length,
        'updatedConversations': resp.updatedConversationIds.length,
      });
      DebugLogManager.logEvent('sync_completed_phone_first', {
        'newConversations': resp.newConversationIds.length,
        'updatedConversations': resp.updatedConversationIds.length,
      });
      return resp;
    }

    // Phase 1: New multi-file storage sync (for new firmware with LittleFS)
    // Refresh file list from device via BLE.
    await _storageSync.refreshWalsFromDevice();
    final storageMissing = await _storageSync.getMissingWals();
    if (storageMissing.isNotEmpty) {
      Logger.debug("WalSyncs: Phase 1 - Downloading ${storageMissing.length} multi-file storage files to phone");
      DebugLogManager.logInfo('Sync Phase 1: Multi-file storage sync');
      progress?.onWalSyncedProgress(0.0, phase: SyncPhase.downloadingFromDevice);
      await _storageSync.syncAll(progress: progress);
    }

    if (_isCancelled) {
      Logger.debug("WalSyncs: Cancelled after storage sync phase");
      return resp;
    }

    // Phase 2a: Download SD card data to phone (legacy firmware)
    Logger.debug("WalSyncs: Phase 2a - Downloading SD card data to phone");
    DebugLogManager.logInfo('Sync Phase 2a: Downloading SD card data to phone');
    progress?.onWalSyncedProgress(0.0, phase: SyncPhase.downloadingFromDevice);
    final missingSDCardWals = (await _sdcardSync.getMissingWals()).where((w) => w.status == WalStatus.miss).toList();

    bool usedWifi = false;
    if (missingSDCardWals.isNotEmpty) {
      final preferredMethod = SharedPreferencesUtil().preferredSyncMethod;
      final wifiSupported = await _sdcardSync.isWifiSyncSupported();

      if (preferredMethod == 'wifi' && wifiSupported) {
        usedWifi = true;
        DebugLogManager.logInfo('SD card sync using WiFi', {'walCount': missingSDCardWals.length});
        await _sdcardSync.syncWithWifi(progress: progress, connectionListener: connectionListener);
      } else {
        DebugLogManager.logInfo('SD card sync using BLE', {'walCount': missingSDCardWals.length});
        await _sdcardSync.syncAll(progress: progress);
      }
    }

    if (_isCancelled) {
      Logger.debug("WalSyncs: Cancelled after SD card phase");
      DebugLogManager.logWarning('Sync cancelled after SD card phase');
      return resp;
    }

    // Phase 2b: Download flash page data to phone
    Logger.debug("WalSyncs: Phase 2b - Downloading flash page data to phone");
    DebugLogManager.logInfo('Sync Phase 2b: Downloading flash page data to phone');
    await _flashPageSync.syncAll(progress: progress);

    if (_isCancelled) {
      Logger.debug("WalSyncs: Cancelled after flash page phase");
      DebugLogManager.logWarning('Sync cancelled after flash page phase');
      return resp;
    }

    if (usedWifi) {
      Logger.debug("WalSyncs: Waiting for internet after WiFi transfer...");
      DebugLogManager.logInfo('Waiting for internet after WiFi transfer');
      progress?.onWalSyncedProgress(0.0, phase: SyncPhase.waitingForInternet);
      await _waitForInternet();
    }

    if (_isCancelled) {
      Logger.debug("WalSyncs: Cancelled after waiting for internet");
      DebugLogManager.logWarning('Sync cancelled while waiting for internet');
      return resp;
    }

    // Phase 3: Upload files downloaded during this sync.
    await _uploadPhoneFilesToCloud(resp, progress, logLabel: 'Sync Phase 3: Uploading downloaded phone files to cloud');

    DebugLogManager.logEvent('sync_completed', {
      'newConversations': resp.newConversationIds.length,
      'updatedConversations': resp.updatedConversationIds.length,
    });

    return resp;
  }

  Future<void> _uploadPhoneFilesToCloud(
    SyncLocalFilesResponse resp,
    IWalSyncProgressListener? progress, {
    required String logLabel,
  }) async {
    Logger.debug("WalSyncs: $logLabel");
    DebugLogManager.logInfo(logLabel);
    progress?.onWalSyncedProgress(0.0, phase: SyncPhase.uploadingToCloud);
    final partialResp = await _phoneSync.syncAll(progress: progress);
    _mergeSyncResponse(resp, partialResp);
    if (!identical(_accumulatedResponse, resp)) {
      _mergeSyncResponse(_accumulatedResponse, partialResp);
    }
  }

  void _mergeSyncResponse(SyncLocalFilesResponse? target, SyncLocalFilesResponse? source) {
    if (target == null || source == null) return;

    target.newConversationIds.addAll(source.newConversationIds.where((id) => !target.newConversationIds.contains(id)));
    target.updatedConversationIds.addAll(
      source.updatedConversationIds.where(
        (id) => !target.updatedConversationIds.contains(id) && !target.newConversationIds.contains(id),
      ),
    );
  }

  @override
  Future<SyncLocalFilesResponse?> syncWal({
    required Wal wal,
    IWalSyncProgressListener? progress,
    IWifiConnectionListener? connectionListener,
  }) async {
    if (wal.storage == WalStorage.sdcard && _storageSync.ownsWal(wal)) {
      progress?.onWalSyncedProgress(0.0, phase: SyncPhase.downloadingFromDevice);
      return _storageSync.syncWal(wal: wal, progress: progress);
    } else if (wal.storage == WalStorage.sdcard) {
      progress?.onWalSyncedProgress(0.0, phase: SyncPhase.downloadingFromDevice);
      final preferredMethod = SharedPreferencesUtil().preferredSyncMethod;
      final wifiSupported = await _sdcardSync.isWifiSyncSupported();

      if (preferredMethod == 'wifi' && wifiSupported) {
        return await _sdcardSync.syncWithWifi(progress: progress, connectionListener: connectionListener);
      } else {
        return _sdcardSync.syncWal(wal: wal, progress: progress);
      }
    } else if (wal.storage == WalStorage.flashPage) {
      progress?.onWalSyncedProgress(0.0, phase: SyncPhase.downloadingFromDevice);
      return _flashPageSync.syncWal(wal: wal, progress: progress);
    } else {
      progress?.onWalSyncedProgress(0.0, phase: SyncPhase.uploadingToCloud);
      return _phoneSync.syncWal(wal: wal, progress: progress);
    }
  }

  @override
  void cancelSync() {
    _isCancelled = true;
    _storageSync.cancelSync();
    _sdcardSync.cancelSync();
    _flashPageSync.cancelSync();
    _phoneSync.cancelSync();
  }

  bool get isStorageSyncing => _storageSync.isSyncing;

  double get storageSpeedKBps => _storageSync.currentSpeedKBps;

  bool get isSdCardSyncing => _sdcardSync.isSyncing;

  double get sdCardSpeedKBps => _sdcardSync.currentSpeedKBps;

  bool get isFlashPageSyncing => _flashPageSync.isSyncing;

  /// Get conversation IDs accumulated so far from completed upload batches.
  /// Returns null if no sync is in progress or no batches have completed.
  SyncLocalFilesResponse? get accumulatedResponse {
    final phoneResponse = _phoneSync.accumulatedResponse;
    if (_accumulatedResponse == null) return phoneResponse;
    _mergeSyncResponse(_accumulatedResponse, phoneResponse);
    return _accumulatedResponse;
  }

  /// Wait for internet connectivity to be restored (e.g. after WiFi transfer).
  /// Polls every 2 seconds, gives up after 30 seconds.
  Future<void> _waitForInternet() async {
    final connectivity = ConnectivityService();
    for (int i = 0; i < 15; i++) {
      if (connectivity.isConnected) {
        Logger.debug("WalSyncs: Internet available after ${i * 2}s");
        DebugLogManager.logInfo('Internet restored after ${i * 2}s');
        return;
      }
      await Future.delayed(const Duration(seconds: 2));
    }
    Logger.debug("WalSyncs: Internet not available after 30s, proceeding anyway");
    DebugLogManager.logWarning('Internet not available after 30s, proceeding anyway');
  }
}
