#include "rtc.h"

#include <errno.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "settings.h"

LOG_MODULE_REGISTER(rtc, CONFIG_LOG_DEFAULT_LEVEL);

static uint64_t base_epoch_ms;
static int64_t base_uptime_ms;
static bool utc_valid;
static uint64_t pending_epoch_to_persist;
static struct k_work rtc_persist_work;
static struct k_mutex rtc_lock;

static void rtc_persist_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);

    uint64_t epoch_s;

    k_mutex_lock(&rtc_lock, K_FOREVER);
    epoch_s = pending_epoch_to_persist;
    k_mutex_unlock(&rtc_lock);

    int err = app_settings_save_rtc_epoch(epoch_s);
    if (err) {
        LOG_ERR("Failed to persist rtc_epoch (err %d)", err);
    }
}

bool rtc_is_valid(void)
{
    bool valid;

    k_mutex_lock(&rtc_lock, K_FOREVER);
    valid = utc_valid;
    k_mutex_unlock(&rtc_lock);

    return valid;
}

uint64_t rtc_get_utc_time_ms(void)
{
    int64_t now_uptime_ms;
    int64_t delta_ms;
    uint64_t now_ms;

    k_mutex_lock(&rtc_lock, K_FOREVER);
    if (!utc_valid) {
        k_mutex_unlock(&rtc_lock);
        return 0;
    }

    now_uptime_ms = k_uptime_get();
    delta_ms = now_uptime_ms - base_uptime_ms;
    if (delta_ms < 0) {
        delta_ms = 0;
    }
    now_ms = base_epoch_ms + (uint64_t) delta_ms;
    k_mutex_unlock(&rtc_lock);

    return now_ms;
}

int rtc_set_utc_time(uint64_t utc_epoch_s)
{
    int err;

    if (utc_epoch_s == 0) {
        return -EINVAL;
    }

    err = rtc_set_utc_time_ms(utc_epoch_s * 1000ULL);
    if (err) {
        return err;
    }

    k_mutex_lock(&rtc_lock, K_FOREVER);
    pending_epoch_to_persist = utc_epoch_s;
    k_mutex_unlock(&rtc_lock);

    k_work_submit(&rtc_persist_work);
    return 0;
}

int rtc_set_utc_time_ms(uint64_t utc_epoch_ms)
{
    if (utc_epoch_ms == 0) {
        return -EINVAL;
    }

    k_mutex_lock(&rtc_lock, K_FOREVER);
    base_epoch_ms = utc_epoch_ms;
    base_uptime_ms = k_uptime_get();
    utc_valid = true;
    k_mutex_unlock(&rtc_lock);

    return 0;
}

uint32_t get_utc_time(void)
{
    uint64_t now_ms = rtc_get_utc_time_ms();
    uint64_t now_s;

    if (now_ms == 0) {
        return 0;
    }

    now_s = now_ms / 1000ULL;
    if (now_s > UINT32_MAX) {
        return UINT32_MAX;
    }

    return (uint32_t) now_s;
}

void init_rtc(void)
{
    static bool initialized;
    uint64_t saved_epoch_s;

    if (!initialized) {
        k_mutex_init(&rtc_lock);
        k_work_init(&rtc_persist_work, rtc_persist_work_handler);
        initialized = true;
    }

    saved_epoch_s = app_settings_get_rtc_epoch();
    if (saved_epoch_s == 0) {
        k_mutex_lock(&rtc_lock, K_FOREVER);
        utc_valid = false;
        k_mutex_unlock(&rtc_lock);
        LOG_WRN("RTC not synchronized yet");
        return;
    }

    k_mutex_lock(&rtc_lock, K_FOREVER);
    base_epoch_ms = saved_epoch_s * 1000ULL;
    base_uptime_ms = k_uptime_get();
    utc_valid = true;
    k_mutex_unlock(&rtc_lock);

    LOG_INF("RTC restored from persisted epoch");
}
